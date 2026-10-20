"""The connector's half of host access: putting the dashboard's public key where ssh will read it.

The dashboard never dials this machine and holds no password for it. What it holds is a private
key, and what makes it work is a line in this machine's `authorized_keys` — written here, by the
connector, running as the account it was installed for (docs/design.md §24). That is the whole
mechanism: the user approves a host in the dashboard, and the host's own connector carries out
the part that only the host can do.

Three properties this module holds to:

- **It only ever touches its own line.** The key is recognised by the comment `csd-agent:<label>`,
  and this file will not edit a key it did not write. Somebody else's `authorized_keys` entries —
  including the operator's own, added by hand — are not this package's business.
- **A failure is never an exception.** `csd-agent` runs inside an agent host's life, on a timer;
  a read-only home directory must produce a line in the dashboard's host list, not a crashed
  heartbeat. Same rule as every other report here (design.md §6).
- **Nothing is assumed to be already there.** A host rebuilt from a backup comes back with an
  empty `authorized_keys` and a `ready` row in the dashboard; the check is therefore by key, not
  by a flag this package remembered, so it repairs itself on the next heartbeat.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, host, runtime

#: The comment every line this package writes carries. It is how the one line we own is found
#: again — for a repair after a restore, and for removal after a revoke.
COMMENT_PREFIX = "csd-agent:"


def comment_for(label: str) -> str:
    return f"{COMMENT_PREFIX}{label}"


def authorized_keys_path() -> Path:
    """`~/.ssh/authorized_keys` for the user this runs as.

    The default, deliberately: honouring a configured `AuthorizedKeysFile` would mean reading
    `sshd_config`, which a user-level process usually cannot, and a wrong guess there is worse
    than the honest assumption that the default is in use.
    """
    return Path.home() / ".ssh" / "authorized_keys"


def _blob(public_key: str) -> str:
    """The base64 body of a public key line — the part that identifies the key.

    Compared rather than the whole line, because the comment is ours while the blob is the
    dashboard's, and a line that carries the same key under someone else's comment is already
    doing the job.
    """
    fields = public_key.split()
    return fields[1] if len(fields) > 1 else public_key.strip()


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OSError(f"could not read {path}: {exc}") from exc


def _write_lines(path: Path, lines: list[str]) -> None:
    """Replace the file with `lines`, atomically, at the mode ssh insists on.

    `mkdir 700` / file 0600 because sshd refuses a group- or world-writable `authorized_keys`
    *silently* — it simply ignores the file, and the symptom ("the dashboard says ready, ssh
    asks for a password") points nowhere near the cause.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".csd-tmp")
    body = "\n".join(lines)
    tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
    _chmod(tmp, 0o600)
    tmp.replace(path)
    _chmod(path, 0o600)


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        # Best effort: Windows has no meaningful mode bits and some filesystems refuse
        # chmod outright. The install still works where it matters, and failing here would
        # turn a cosmetic problem into "the key was never written".
        pass


def _line_for(public_key: str, label: str) -> str:
    """The `authorized_keys` line for this key: type, blob, and **our** comment.

    Rebuilt rather than pasted, because the dashboard sends the key with a comment of its own
    (`ssh-keygen -C`), and appending a second one produces a line sshd accepts but no one can
    read — and would leave the recognisable form different from what `comment_for` searches for.
    """
    fields = public_key.split()
    if len(fields) < 2:
        raise ValueError(f"not a public key: {public_key[:40]!r}")
    return f"{fields[0]} {fields[1]} {comment_for(label)}"


def install_key(public_key: str, label: str) -> str:
    """Make sure this key is authorized. Returns a sentence describing what happened.

    Raises `OSError` only when the file itself cannot be written — the caller turns that into a
    report, because the user needs to see *why* the host never went ready.
    """
    path = authorized_keys_path()
    lines = _read_lines(path)
    blob = _blob(public_key)
    comment = comment_for(label)
    line = _line_for(public_key, label)

    for index, existing in enumerate(lines):
        stripped = existing.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _blob(stripped) == blob:
            if stripped.split()[-1] == comment:
                return f"already authorized in {path}"
            # Same key, someone else's comment: it is already in force, and rewriting it would
            # be this package editing a line it did not write.
            return f"already authorized in {path} under another comment"
        if stripped.split()[-1] == comment:
            # Our line, a different key: this is what a revoke-then-approve looks like. Replace
            # rather than append, or the revoked key stays authorized forever.
            lines[index] = line
            _write_lines(path, lines)
            return f"replaced the previous key in {path}"

    lines.append(line)
    _write_lines(path, lines)
    return f"added the key to {path}"


def remove_key(label: str) -> str:
    """Delete the line this package wrote, if it is there. Never raises."""
    path = authorized_keys_path()
    comment = comment_for(label)
    try:
        lines = _read_lines(path)
    except OSError as exc:
        return str(exc)
    kept = [line for line in lines if line.strip().split()[-1:] != [comment]]
    if len(kept) == len(lines):
        return f"no key of ours in {path}"
    try:
        _write_lines(path, kept)
    except OSError as exc:
        return f"could not update {path}: {exc}"
    return f"removed the key from {path}"


def sync(*, verbose: bool = False) -> runtime.Result:
    """One round: ask what the dashboard wants, do it, say what happened.

    Runs right after `report_host()` on every heartbeat, because the two are the same errand —
    *tell the dashboard where I am, and find out whether it needs anything from me*. Doing it
    here rather than on a timer of its own means an approval takes effect on the next heartbeat
    and nothing new has to be scheduled.
    """
    try:
        cfg = config.AgentConfig.load()
    except FileNotFoundError as exc:
        return runtime.Result(False, detail=str(exc))

    if not cfg.wants_access:
        # Opted out (`--no-drive`). This host is not asking to be driven, so it is not its
        # business to sync — and a key the user authorized in the past is theirs to revoke from
        # the dashboard, not something an install flag quietly deletes.
        return runtime.Result(True, detail="access not requested")

    result = runtime.cpctl("host", "access", "--label", cfg.label,
                           "--agent-type", host.AGENT_TYPE)
    if not result.ok:
        # Unreachable dashboard: nothing to do, and the next heartbeat tries again. Reported
        # rather than raised, like every other network failure in this package.
        if verbose:
            print(f"access     not checked: {result.detail}")
        return result

    payload = _parse(result.stdout)
    state = str(payload.get("state") or "none")
    public_key = str(payload.get("public_key") or "")

    if state == "disabled":
        message = remove_key(cfg.label)
        if verbose:
            print(f"access     {message}")
        return result
    if state not in {"pending", "ready"} or not public_key:
        if verbose:
            print(f"access     {state} — nothing to install")
        return result

    try:
        message = install_key(public_key, cfg.label)
    except (OSError, ValueError) as exc:
        # Reported, not raised: the user sees "the host could not install the key" in the
        # dashboard, which is the difference between a bug they can fix and a host that sits at
        # `pending` forever with no explanation.
        reported = runtime.cpctl(
            "host", "access-report",
            "--label", cfg.label, "--agent-type", host.AGENT_TYPE,
            "--error", str(exc)[:200],
        )
        if verbose:
            print(f"access     FAILED: {exc}")
        return reported if reported.ok else result

    runtime.cpctl(
        "host", "access-report",
        "--label", cfg.label, "--agent-type", host.AGENT_TYPE,
    )
    if verbose:
        print(f"access     {message}")
    return result


def _parse(stdout: str) -> dict:
    """cpctl prints one JSON object; anything else is treated as "no answer"."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "COMMENT_PREFIX",
    "authorized_keys_path",
    "comment_for",
    "install_key",
    "remove_key",
    "sync",
]
