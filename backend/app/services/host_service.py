"""Which agent hosts are connected, and for which coding agent.

The problem this exists for: a connector (`csd-agent`) can be installed, tunnelled, and
working perfectly while the Sessions list shows nothing at all — because every group in that
list was derived from sessions, and a host with nothing running has none. The only way to
answer "did it connect?" was to SSH in and run `csd-agent status`, which is the exact errand
this project exists to remove.

Two rules shape everything here:

- **A host is reported, never inferred** (docs/design.md §2). Nothing in this module derives a
  host from a session, from traffic, or from a connector's silence. `connected` is true because
  a connector said so; `disconnected_at` is set because a connector said goodbye. A host that
  simply stops talking stays connected and goes `possibly_stale`, which is the honest rendering
  of "we cannot ask it".
- **The key is `(label, agent_type)`, not `label`** — one connector speaks for one coding
  agent, so a box running both Claude Code and codex connects twice and those are two facts.
  See the comment on `HostReport` in schemas.py.

`connected_at` is deliberately not rewritten on every ping — only on a real reconnect, when the
row was previously marked disconnected. A value that moved on every heartbeat would mean
"when we last heard from it", which is what `last_seen_at` is already for.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from ..database import Database
from ..schemas import HostOut, HostReport, utcnow
from . import host_access_service as ha
from .session_service import parse_ts


def new_host_id() -> str:
    """Server-generated, like a session id. The prefix keeps `journalctl` lines readable."""
    return f"h_{secrets.token_hex(12)}"


def report_host(db: Database, data: HostReport) -> HostOut:
    """Record that a connector is here (or that it is going away).

    An upsert, because a connector reports on every heartbeat: a table that grew a row per ping
    would make "which hosts are connected" unanswerable, which is the one question it is for.
    """
    now = utcnow()
    with db.write():
        db.execute(
            """
            INSERT INTO agent_hosts (
                id, label, agent_type, connected_at, last_seen_at, disconnected_at,
                wants_access, ssh_host, ssh_user, ssh_port
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (label, agent_type) DO UPDATE SET
                -- Move only on a genuine reconnect: a row that was marked disconnected coming
                -- back. A ping from an already-connected host must not restart the clock, or
                -- "connected since" would just read "last seen".
                connected_at = CASE
                    WHEN agent_hosts.disconnected_at IS NOT NULL
                     AND excluded.disconnected_at IS NULL
                    THEN excluded.connected_at
                    ELSE agent_hosts.connected_at
                END,
                last_seen_at    = excluded.last_seen_at,
                disconnected_at = excluded.disconnected_at,
                -- The declaration is rewritten on every report, including on a heartbeat, for
                -- the same reason `last_seen_at` is: it is what the connector says *now*. The
                -- approval it may or may not still match lives in `host_access` (§24).
                wants_access    = excluded.wants_access,
                ssh_host        = excluded.ssh_host,
                ssh_user        = excluded.ssh_user,
                ssh_port        = excluded.ssh_port
            """,
            (
                new_host_id(),
                data.label,
                data.agent_type,
                now,
                now,
                None if data.connected else now,
                1 if data.wants_access else 0,
                data.ssh_host,
                data.ssh_user,
                data.ssh_port,
            ),
        )

    row = db.query_one(
        "SELECT * FROM agent_hosts WHERE label = ? AND agent_type = ?",
        (data.label, data.agent_type),
    )
    return _row_to_host(row, access_row=ha.get_row(db, row["id"]), possibly_stale=False)


def list_hosts(db: Database, *, stale_after_seconds: int) -> list[HostOut]:
    """Every registered host, most recently seen first.

    Ordered by `last_seen_at` rather than `connected_at`: the list is a liveness view, so the
    box that just checked in belongs at the top, not the one that has been up longest.
    """
    rows = db.query("SELECT * FROM agent_hosts ORDER BY last_seen_at DESC")
    # One query for every approval rather than one per row: the list is small but it is fetched
    # on every page load and on every host frame, and an N+1 here would be invisible until it
    # mattered.
    access = {
        row["host_id"]: row for row in db.query("SELECT * FROM host_access")
    }
    return _with_staleness(
        rows, access=access, stale_after_seconds=stale_after_seconds
    )


def get_host_row(db: Database, host_id: str):
    return db.query_one("SELECT * FROM agent_hosts WHERE id = ?", (host_id,))


def _with_staleness(rows, *, access: dict | None = None, stale_after_seconds: int) -> list[HostOut]:
    access = access or {}
    hosts = [_row_to_host(r, access_row=access.get(r["id"])) for r in rows]
    return [with_staleness(h, stale_after_seconds=stale_after_seconds) for h in hosts]


def with_staleness(host: HostOut, *, stale_after_seconds: int) -> HostOut:
    """The one display hint, in the one place.

    Applied per host so the routes that answer about a single host reach the same rule as the
    list does; a second copy of "how quiet is too quiet" is how one screen starts calling a
    host stale while another calls it connected.
    """
    if stale_after_seconds <= 0 or host.disconnected_at is not None:
        # Only a host that claims to be connected can be *suspiciously* quiet. One that reported
        # its own disconnect is not stale — it is a reported fact, and a reported state must
        # never wear the rendering reserved for a guess.
        return host
    quiet = (datetime.now(timezone.utc) - parse_ts(host.last_seen_at)).total_seconds()
    host.possibly_stale = quiet >= stale_after_seconds
    return host


def host_for(db: Database, host_id: str, *, stale_after_seconds: int) -> HostOut | None:
    """One host by id, with its access state and the display hint — what every route that
    answers about a single host returns. `None` means the id is unknown, which is a 404 rather
    than an empty host."""
    row = get_host_row(db, host_id)
    if row is None:
        return None
    return with_staleness(
        _row_to_host(row, access_row=ha.get_row(db, host_id)),
        stale_after_seconds=stale_after_seconds,
    )


def _row_to_host(row, *, access_row=None, possibly_stale: bool = False) -> HostOut:
    info = ha.access_info(row, access_row)
    return HostOut(
        id=row["id"],
        label=row["label"],
        agent_type=row["agent_type"],
        connected_at=row["connected_at"],
        last_seen_at=row["last_seen_at"],
        disconnected_at=row["disconnected_at"],
        possibly_stale=possibly_stale,
        wants_access=bool(row["wants_access"]),
        access_state=info.state,
        access_target=info.target,
        access_fingerprint=info.fingerprint,
    )


__all__ = [
    "get_host_row",
    "host_for",
    "list_hosts",
    "new_host_id",
    "report_host",
    "with_staleness",
]
