"""Host access: the user's decision that the personal agent may ssh into a registered machine.

The problem this exists for: a coding agent's host dials *in* to this dashboard, so connecting
proves the host can reach us and says nothing about whether we can reach it — and the dashboard
has never held an ssh credential for it. Until now the only way to let the agent drive such a
machine was to hand-place a key and write the address into `MACHINES.md`, which is an errand
per host and invisible to the registry the rail already draws.

Two facts are kept strictly apart, and that separation is the whole design (docs/design.md §24):

- **Declared** — what the connector says about itself: "I can be driven, and I believe I am
  reachable at X". It rides in on the shared ingest token, so any host could say anything.
- **Approved** — a row in `host_access`, which only a person can create, holding the exact
  address the user accepted and the key the agent ssh's with.

Collapsing them would make a self-report enough to hand out a shell. Keeping them apart also
gives the one staleness rule worth having: if the connector's *declaration* moves away from
what it declared at approval time, the approval is no longer about the same machine, and the
host drops to `address_changed` until the user looks again. Overriding the address by hand is
not a move — `seen_*` records the declaration, not the choice.

**The private key never leaves this side.** It is generated here, written under the agent's
`$HOME/.ssh` at 0600, and referenced by path. `connector_payload` — the only thing a connector
ever receives — carries the public half and nothing else.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..database import Database
from ..schemas import (
    AccessState,
    HostAccessFetchOut,
    HostAccessOut,
    HostOut,
    utcnow,
)

logger = logging.getLogger("csd.hosts.access")

#: Stored states. `address_changed` and `requested` are computed, never written — see
#: `state_for()`, and the table comment in database.py for why.
PENDING = "pending"
READY = "ready"
DISABLED = "disabled"

#: Prefix of the `authorized_keys` comment our key carries. The connector uses it to find the
#: one line it owns: it may append that line and delete that line, and must not touch another.
#: The dashboard does not act on the remote file at all; this is the connector's contract.
COMMENT_PREFIX = "csd-agent:"

#: `ssh-keygen` is the tool that knows every key format ssh accepts. Generating keys in Python
#: would mean either a crypto dependency this project does not have, or hand-rolling ed25519.
SSH_KEYGEN = "ssh-keygen"


class AccessError(Exception):
    """A refusal a person should read: no `ssh-keygen`, no address to approve, and so on."""


@dataclass(frozen=True)
class AccessInfo:
    """The parts of an access row the read paths need, without the private key's path.

    `key_path` deliberately lives outside this dataclass: it is needed to *revoke* and to write
    the agent's machines file, and neither of those goes through the API layer's vocabulary.
    """

    state: AccessState | None
    target: str | None
    fingerprint: str | None


def declared_for(host_row) -> tuple[str | None, str | None, int | None]:
    """The connector's declared address, or all-`None` when it declared nothing.

    A port of 22 is the schema's default and is *not* treated as a declaration when no host was
    given: an old connector that sends nothing must not look like a host that declared
    `(None, None, 22)` and then changed. Public because the machines file prints it — the agent
    should see what the host claims, next to what the user approved.
    """
    host = host_row["ssh_host"] or None
    user = host_row["ssh_user"] or None
    if host is None and user is None:
        return (None, None, None)
    return (host, user, host_row["ssh_port"] or 22)


#: Kept as the internal name too: every comparison in this module reads better as `_declared`.
_declared = declared_for


def _seen(row) -> tuple[str | None, str | None, int | None]:
    return (row["seen_host"], row["seen_user"], row["seen_port"])


def state_for(host_row, access_row) -> AccessState | None:
    """What the agent may do with this host right now.

    `None` means the connector never asked to be driven, which is a different fact from
    "asked and was refused" — the UI and the agent both need to tell those apart.
    """
    if access_row is None:
        return "requested" if host_row["wants_access"] else None
    if access_row["state"] == DISABLED:
        return "disabled"
    if _declared(host_row) != _seen(access_row):
        return "address_changed"
    return access_row["state"]  # pending | ready


def access_info(host_row, access_row) -> AccessInfo:
    """The approval part of a host, from rows already in hand. One place, so every read path
    agrees on what `ready` means."""
    live = access_row is not None and access_row["state"] != DISABLED
    return AccessInfo(
        state=state_for(host_row, access_row),
        target=target_for(access_row) if live else None,
        fingerprint=access_row["fingerprint"] if access_row is not None else None,
    )


def get_row(db: Database, host_id: str):
    return db.query_one("SELECT * FROM host_access WHERE host_id = ?", (host_id,))


def get_row_for_label(db: Database, label: str, agent_type: str):
    """The access row for a connector that identifies itself by `(label, agent_type)`.

    A join rather than a stored label on `host_access`, because the host id is the identity and
    a copied label would be a second one.
    """
    return db.query_one(
        """
        SELECT host_access.* FROM host_access
        JOIN agent_hosts ON agent_hosts.id = host_access.host_id
        WHERE agent_hosts.label = ? AND agent_hosts.agent_type = ?
        """,
        (label, agent_type),
    )


def target_for(row) -> str | None:
    """`user@host:port`, the string a person reads and the agent's ssh line is built from."""
    if row is None:
        return None
    return f"{row['ssh_user']}@{row['ssh_host']}:{row['ssh_port']}"


def comment_for(label: str) -> str:
    return f"{COMMENT_PREFIX}{label}"


# --------------------------------------------------------------------------- #
# authorizing
# --------------------------------------------------------------------------- #
def authorize(
    db: Database,
    host_row,
    *,
    key_dir: Path,
    ssh_host: str | None = None,
    ssh_user: str | None = None,
    ssh_port: int | None = None,
) -> HostAccessOut:
    """Approve this host, generating a key on first approval.

    Idempotent for a host that is already approved: re-approving is how a person confirms a
    moved address, and generating a second key would leave the first one authorized on the
    remote forever. A `disabled` row is the exception — revocation deleted the key file, so
    approving again mints a new one.
    """
    label = host_row["label"]
    declared_host, declared_user, declared_port = _declared(host_row)

    host = (ssh_host or declared_host or label).strip()
    user = (ssh_user or declared_user or "").strip()
    port = int(ssh_port or declared_port or 22)
    if not user:
        # Refusing beats inventing: guessing `root` (or the dashboard's own user) would aim a
        # shell at an account nobody named.
        raise AccessError(
            "this host declared no ssh user; pass ssh_user (the account the connector runs as)"
        )

    existing = get_row(db, host_row["id"])
    now = utcnow()

    if existing is not None and existing["state"] != DISABLED:
        with db.write():
            db.execute(
                """
                UPDATE host_access
                   SET ssh_host = ?, ssh_user = ?, ssh_port = ?,
                       seen_host = ?, seen_user = ?, seen_port = ?,
                       updated_at = ?
                 WHERE host_id = ?
                """,
                (host, user, port, *_declared(host_row), now, host_row["id"]),
            )
        row = get_row(db, host_row["id"])
        return HostAccessOut(
            host=HostOut(**host_fields(host_row, row)),
            public_key=row["public_key"],
            fingerprint=row["fingerprint"],
            note=(
                "address updated; the key is unchanged and already installed"
                if existing["state"] == READY
                else "waiting for the host to install the key on its next heartbeat"
            ),
        )

    key_path, public_key, fingerprint = _generate_keypair(key_dir, label, host_row["agent_type"])
    if existing is None:
        with db.write():
            db.execute(
                """
                INSERT INTO host_access (
                    host_id, ssh_host, ssh_user, ssh_port,
                    seen_host, seen_user, seen_port,
                    key_path, public_key, fingerprint, state,
                    created_at, updated_at, installed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    host_row["id"], host, user, port,
                    *_declared(host_row),
                    str(key_path), public_key, fingerprint, PENDING,
                    now, now,
                ),
            )
    else:
        with db.write():
            db.execute(
                """
                UPDATE host_access
                   SET ssh_host = ?, ssh_user = ?, ssh_port = ?,
                       seen_host = ?, seen_user = ?, seen_port = ?,
                       key_path = ?, public_key = ?, fingerprint = ?,
                       state = ?, last_error = NULL, updated_at = ?, installed_at = NULL
                 WHERE host_id = ?
                """,
                (
                    host, user, port, *_declared(host_row),
                    str(key_path), public_key, fingerprint, PENDING, now, host_row["id"],
                ),
            )

    row = get_row(db, host_row["id"])
    return HostAccessOut(
        host=HostOut(**host_fields(host_row, row)),
        public_key=public_key,
        fingerprint=fingerprint,
        note="waiting for the host to install the key on its next heartbeat",
    )


def revoke(db: Database, host_row) -> HostOut:
    """Withdraw the approval and delete the private key.

    The remote's `authorized_keys` line is removed by the connector, which is the only side
    that can: this service has no way in once the key is gone, and that is the point.
    """
    row = get_row(db, host_row["id"])
    if row is None:
        raise AccessError("this host has no access to revoke")
    _remove_key_files(Path(row["key_path"]))
    with db.write():
        db.execute(
            "UPDATE host_access SET state = ?, updated_at = ? WHERE host_id = ?",
            (DISABLED, utcnow(), host_row["id"]),
        )
    return HostOut(**host_fields(host_row, get_row(db, host_row["id"])))


# --------------------------------------------------------------------------- #
# the connector's half
# --------------------------------------------------------------------------- #
def connector_payload(db: Database, label: str, agent_type: str) -> HostAccessFetchOut:
    """What a connector is told when it asks about its own access.

    `ready` returns the key too, not because it needs installing again — it is already there —
    but because "is my line still in `authorized_keys`?" is a question the connector can only
    answer against the key itself, and a host rebuilt from a backup should repair itself
    without waiting for a person to re-approve.
    """
    row = get_row_for_label(db, label, agent_type)
    if row is None or row["state"] == DISABLED:
        return HostAccessFetchOut(state=DISABLED if row is not None else "none")
    return HostAccessFetchOut(
        state=row["state"],
        public_key=row["public_key"],
        comment=comment_for(label),
    )


def mark_installed(
    db: Database, label: str, agent_type: str, *, installed: bool, error: str | None
) -> HostAccessFetchOut:
    """The connector's report: the public key is in this machine's `authorized_keys`.

    A failure keeps the row at `pending` and records why. It is not retried from here — the
    connector will try again on its next heartbeat, and a state that flipped to `ready` on a
    report we did not get would be a lie the agent acts on.
    """
    row = get_row_for_label(db, label, agent_type)
    if row is None or row["state"] == DISABLED:
        return HostAccessFetchOut(state=DISABLED if row is not None else "none")

    now = utcnow()
    if installed:
        with db.write():
            db.execute(
                """
                UPDATE host_access
                   SET state = ?, last_error = NULL, updated_at = ?, installed_at = ?
                 WHERE host_id = ?
                """,
                (READY, now, now, row["host_id"]),
            )
    else:
        with db.write():
            db.execute(
                "UPDATE host_access SET last_error = ?, updated_at = ? WHERE host_id = ?",
                (error or "the host could not install the key", now, row["host_id"]),
            )
    return HostAccessFetchOut(state=get_row(db, row["host_id"])["state"])


def host_out(host_row, access_row=None) -> HostOut:
    """One host as the API and the stream describe it. `possibly_stale` is the caller's to
    apply (`host_service.with_staleness`), because the threshold is configuration."""
    return HostOut(**host_fields(host_row, access_row))


# --------------------------------------------------------------------------- #
# keys on disk
# --------------------------------------------------------------------------- #
def host_fields(host_row, access_row) -> dict:
    """`HostOut` kwargs for a host row plus its access row."""
    info = access_info(host_row, access_row)
    return {
        "id": host_row["id"],
        "label": host_row["label"],
        "agent_type": host_row["agent_type"],
        "connected_at": host_row["connected_at"],
        "last_seen_at": host_row["last_seen_at"],
        "disconnected_at": host_row["disconnected_at"],
        "wants_access": bool(host_row["wants_access"]),
        "access_state": info.state,
        "access_target": info.target,
        "access_fingerprint": info.fingerprint,
    }


def _key_name(label: str, agent_type: str) -> str:
    """A file name that is safe on every filesystem and unique per approval.

    The random suffix is what makes re-approving after a revoke a *new* key rather than a
    silent reuse of a file that was deleted a moment ago.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{label}_{agent_type}").strip("-")[:48]
    return f"csd_{safe or 'host'}_{secrets.token_hex(4)}"


def _generate_keypair(key_dir: Path, label: str, agent_type: str) -> tuple[Path, str, str]:
    if shutil.which(SSH_KEYGEN) is None:
        raise AccessError(
            f"{SSH_KEYGEN} is not installed, so no key can be generated; install OpenSSH or "
            "place a key by hand and set CSD_AGENT_KEY_DIR"
        )

    key_dir.mkdir(parents=True, exist_ok=True)
    try:
        key_dir.chmod(0o700)
    except OSError:
        # Windows dev has no meaningful mode bits; the failure is worth nothing and the key is
        # still created. Deployments where it matters are POSIX, where this succeeds.
        pass

    path = key_dir / _key_name(label, agent_type)
    comment = comment_for(label)
    proc = subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 or not path.exists():
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200]
        raise AccessError(f"{SSH_KEYGEN} failed: {detail}")

    try:
        path.chmod(0o600)
    except OSError:
        pass

    public_key = Path(f"{path}.pub").read_text(encoding="utf-8").strip()
    fingerprint = _fingerprint(path)
    logger.info("generated an ssh key for %s/%s at %s", label, agent_type, path)
    return path, public_key, fingerprint


def _fingerprint(path: Path) -> str:
    """`ssh-keygen -lf`, or a readable stand-in if that fails.

    A fingerprint is display furniture; a missing one must not be the reason an approval
    cannot be recorded.
    """
    try:
        proc = subprocess.run(
            [SSH_KEYGEN, "-lf", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # "256 SHA256:abc… csd-agent:lab-box (ED25519)" → the middle field.
            parts = proc.stdout.split()
            return parts[1] if len(parts) > 1 else proc.stdout.strip()[:64]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _remove_key_files(key_path: Path) -> None:
    """Delete the private key and its `.pub`. Missing files are fine — revocation is idempotent."""
    for path in (key_path, Path(f"{key_path}.pub")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("could not remove %s: %s", path, exc)


__all__ = [
    "AccessError",
    "AccessInfo",
    "COMMENT_PREFIX",
    "DISABLED",
    "PENDING",
    "READY",
    "access_info",
    "authorize",
    "comment_for",
    "connector_payload",
    "declared_for",
    "get_row",
    "get_row_for_label",
    "host_fields",
    "host_out",
    "mark_installed",
    "revoke",
    "state_for",
    "target_for",
]
