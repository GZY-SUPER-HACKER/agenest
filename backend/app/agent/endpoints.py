"""Where the personal agent said it can be reached, and the one rule about which places count.

**Why this exists.** This service is always the side that dials — it is a *client* of an agent,
never a server for one (§17). So "my nanobot connected itself to agenest" cannot mean the agent
opened a socket to us; it means the agent opened a door on its own machine and then said where
that door is. This module is that sentence: a row per agent kind, plus a small process-local
cache so the backends can ask "where am I?" without touching SQLite on every status render.

**Why loopback only, and why that is the whole security story.** A personal agent is a local
component of this control plane (CLAUDE.md, "Two kinds of agent") — it runs on the same machine
as this service, always. A registration naming any other host is therefore either a mistake or
somebody pointing the agent page at a server of theirs, which would hand them everything the
user types. Refusing every non-loopback URL costs a legitimate deployment nothing and removes
that whole class of problem without inventing a second credential (§22, §25).

**Why the token lives in the database.** It is the *agent's* credential for its own local port
(nanobot's `serve` needs none; Hermes' API server needs one), not a secret of this service's.
It sits beside the database in the same directory, readable by the same user, which is the same
trust boundary as the keys host access writes into `$HOME/.ssh` (§24) — and it never reaches a
response body, a log line, or the prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from ..database import Database
from ..schemas import utcnow

logger = logging.getLogger("csd.agent.endpoints")


@dataclass(frozen=True)
class AgentEndpoint:
    """One agent kind's door, as the agent described it.

    `label` is whatever the agent calls itself (its bot name, its host); it is display text and
    never used for routing, because a name an agent chose for itself must not decide which
    process we talk to.

    `last_error` rides along so a status render can say "registered, but the door did not open"
    without a query per frame: it is written by the register route's probe and read straight
    from this cache.
    """

    kind: str
    url: str
    token: str | None = None
    label: str | None = None
    last_error: str | None = None


#: The process-local copy, keyed by kind. Loaded at startup and rewritten on every change, so a
#: backend asking "where am I?" is a dict lookup rather than a query — a status render happens
#: per frame, and the page asks for one on every connect.
_ENDPOINTS: dict[str, AgentEndpoint] = {}


class EndpointError(ValueError):
    """A registration a person should read: not loopback, not a URL, and so on."""


def validate_url(url: str) -> str:
    """Normalize an agent's door URL, refusing anything that is not on this machine.

    Returns the URL without its trailing slash, so `http://127.0.0.1:8900` and
    `http://127.0.0.1:8900/` cannot become two different rows for one door.
    """
    raw = (url or "").strip()
    if not raw:
        raise EndpointError("url must not be blank")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise EndpointError("url must start with http:// or https://")
    host = parts.hostname
    if not host:
        raise EndpointError(f"{raw!r} names no host")
    if not _is_loopback(host):
        # One sentence, and the reason, because "invalid url" would send someone to check
        # their spelling when the answer is that this must be the machine we are on.
        raise EndpointError(
            f"{host!r} is not this machine. A personal agent runs on the same host as this "
            "service (a local component, not a remote one), so only 127.0.0.1, ::1 and "
            "localhost can be registered."
        )
    return raw.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        # A name that is not `localhost`. Resolving it would make this check depend on DNS —
        # and on a machine where `mybox.internal` happens to resolve to 127.0.0.1, the answer
        # would change with the network. Refusing is stable.
        return False


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def load(db: Database) -> dict[str, AgentEndpoint]:
    """Read every row into the cache. Called at startup and after each change."""
    rows = db.query("SELECT kind, url, token, label, last_error FROM agent_endpoints")
    _ENDPOINTS.clear()
    for row in rows:
        _ENDPOINTS[row["kind"]] = AgentEndpoint(
            kind=row["kind"],
            url=row["url"],
            token=row["token"],
            label=row["label"],
            last_error=row["last_error"],
        )
    if _ENDPOINTS:
        logger.info("registered agent endpoints: %s", ", ".join(sorted(_ENDPOINTS)))
    return dict(_ENDPOINTS)


def get(kind: str) -> AgentEndpoint | None:
    return _ENDPOINTS.get(kind)


def clear() -> None:
    """Drop the cache.

    For tests, which build several apps in one process and must not inherit the previous one's
    registrations — the cache is process-global on purpose (a status render happens per frame,
    and a query per frame would be silly), so something has to be able to empty it.
    """
    _ENDPOINTS.clear()


def all() -> dict[str, AgentEndpoint]:
    return dict(_ENDPOINTS)


def put(db: Database, endpoint: AgentEndpoint) -> AgentEndpoint:
    """Insert or replace one kind's door, then refresh the cache.

    Replace, not merge: an agent that re-registers with a blank token means "there is no token
    on this door now", and keeping the old one would leave us presenting a credential the agent
    no longer expects.
    """
    now = utcnow()
    with db.write():
        db.execute(
            """
            INSERT INTO agent_endpoints (kind, url, token, label, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (kind) DO UPDATE SET
                url = excluded.url,
                token = excluded.token,
                label = excluded.label,
                updated_at = excluded.updated_at,
                -- A re-registration is a new door: the previous knock's verdict is about a
                -- different address and would read as a failure of this one.
                last_error = NULL,
                last_probe_at = NULL
            """,
            (endpoint.kind, endpoint.url, endpoint.token, endpoint.label, now, now),
        )
    load(db)
    return endpoint


def remove(db: Database, kind: str) -> bool:
    """Forget one kind. Returns whether there was anything to forget."""
    with db.write():
        cursor = db.execute("DELETE FROM agent_endpoints WHERE kind = ?", (kind,))
    removed = cursor.rowcount > 0
    load(db)
    return removed


def record_probe(db: Database, kind: str, *, error: str | None) -> None:
    """Remember whether the door answered the last time we knocked.

    Kept in the row rather than only in the backend's memory because it is the *user's* next
    question ("did that work?") and the answer should survive a service restart — an agent
    that registered against a shut door should still say so tomorrow morning.
    """
    with db.write():
        db.execute(
            "UPDATE agent_endpoints SET last_probe_at = ?, last_error = ? WHERE kind = ?",
            (utcnow(), error, kind),
        )
    # Refresh the cache, or the backend reading it would keep showing the previous verdict.
    load(db)


__all__ = [
    "AgentEndpoint",
    "EndpointError",
    "all",
    "get",
    "load",
    "put",
    "record_probe",
    "remove",
    "validate_url",
]
