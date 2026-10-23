"""SQLite access.

Choices, and why:
- **Raw sqlite3, no ORM.** The spec asks for "SQLAlchemy or lightweight SQL"; this workload
  is two tables with a handful of queries. An ORM would add a dependency and a concept
  (sessions, identity map) without removing any logic.
- **WAL mode.** The API serves reads (SSE clients, dashboard) while the ingest API writes.
  WAL lets readers proceed without blocking the writer.
- **One connection per thread, and one writer at a time.** FastAPI runs sync endpoints in a
  threadpool, so a single `Database` object is used from several threads at once. A sqlite3
  connection cannot be: sharing one interleaves two requests' implicit transactions, and the
  failure is not merely an error — a `rollback()` issued by one request can undo another's
  `UPDATE` while that request's own `INSERT` still commits. That is how a session reported
  `completed` was left `running` with a `status_changed → completed` event on its timeline
  (§35). So the connection is thread-local, and `write()` opens it with `BEGIN IMMEDIATE` so
  two writers queue instead of interleaving.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    agent_type        TEXT NOT NULL,
    title             TEXT NOT NULL,
    project_name      TEXT,
    workspace         TEXT,
    -- Which machine the work ran on, as the reporter named it. Nullable on purpose: rows
    -- reported before hosts existed have no answer, and a reporter that was not told one must
    -- not invent it. `None` groups separately in the UI rather than being guessed at.
    host              TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    started_at        TEXT NOT NULL,
    last_activity_at  TEXT NOT NULL,
    finished_at       TEXT,
    summary           TEXT,
    exit_code         INTEGER,
    heartbeat_count   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
    ON sessions (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS session_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,
    message      TEXT NOT NULL,
    payload_json TEXT,
    event_key    TEXT,
    occurred_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_occurred
    ON session_events (session_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_events_received
    ON session_events (received_at, id);

-- Idempotency for retried POSTs: one logical event inserts at most one row.
-- NULL keys (the common case) stay unique under SQLite's NULL-distinct semantics.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_event_key
    ON session_events (event_key) WHERE event_key IS NOT NULL;

-- Cursor for SSE reconnect ("give me everything after id N").
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- --------------------------------------------------------------------------
-- The machines that run coding agents (docs/design.md §20).
--
-- Not `sessions`, because a session is work being reported and its status says how that work
-- ended; a host is a machine being reachable, a property with no outcome. See the comment on
-- `HostReport` in schemas.py for why the key is `(label, agent_type)` and not `label`.
--
-- `disconnected_at` is written only when a connector *says* it is going away. A host that
-- stops talking stays non-null-disconnected and goes `possibly_stale`, which is the honest
-- rendering — there is nobody left to ask.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_hosts (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    agent_type      TEXT NOT NULL,
    connected_at    TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    disconnected_at TEXT,
    -- What the connector *declares* about being driven: that it wants the personal agent to
    -- be able to reach it, and where it believes it can be reached. Declaration only — see
    -- `host_access` for the address the user actually approved, and docs/design.md §24 for
    -- why those two must not be the same column.
    wants_access    INTEGER NOT NULL DEFAULT 0,
    ssh_host        TEXT,
    ssh_user        TEXT,
    ssh_port        INTEGER NOT NULL DEFAULT 22,
    UNIQUE (label, agent_type)
);

-- --------------------------------------------------------------------------
-- Host access (docs/design.md §24): the user's decision that the personal agent may ssh
-- into one registered machine, plus the key it ssh's with.
--
-- Separate from `agent_hosts` on purpose. A connector declaring "I can be driven" is not
-- authorization — any holder of the shared ingest token could say it — and collapsing the
-- two would make a self-report enough to hand out a shell. The row here exists only because
-- a person approved this exact address.
--
-- **The private key is not in this table.** Only its path: the file lives in the agent's
-- `$HOME/.ssh`, mode 0600, and never travels through the API, the prompt, or the generated
-- machines file.
--
-- `state` is pending | ready | disabled. `address_changed` is *not* a stored state: it is
-- the live comparison between what the connector declared when the user approved (`seen_*`
-- below) and what it is declaring now, computed per read. Comparing against `ssh_*` instead
-- would call a user's deliberate override a host move, and the two are different events.
--
-- `seen_*` is nullable because a connector that declares no address at all is legitimate (a
-- user typing one in by hand); null-vs-null is "nothing moved".
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS host_access (
    host_id      TEXT PRIMARY KEY REFERENCES agent_hosts(id) ON DELETE CASCADE,
    ssh_host     TEXT NOT NULL,
    ssh_user     TEXT NOT NULL,
    ssh_port     INTEGER NOT NULL DEFAULT 22,
    seen_host    TEXT,
    seen_user    TEXT,
    seen_port    INTEGER,
    key_path     TEXT NOT NULL,
    public_key   TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    state        TEXT NOT NULL,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    installed_at TEXT
);

-- --------------------------------------------------------------------------
-- Where a personal agent said it can be reached (docs/design.md §25).
--
-- The direction matters here and is the reason this table exists at all: this service is
-- always the one that dials, so "my agent connected itself" means the agent opened a door on
-- its own side and then *told us where it is*. This row is that sentence.
--
-- `kind` is the key, not an id: one deployment has one way in per agent kind, and a second
-- row for the same kind would make "which of these is my nanobot" a question the page has to
-- ask. `url` is constrained to loopback in `agent/endpoints.py` — a personal agent is a local
-- component (CLAUDE.md), not a remote service, and refusing the rest is what makes the
-- register endpoint safe without a second credential.
--
-- `token` is the agent's own credential for that door when it has one (nanobot's `serve` has
-- none on loopback; Hermes' API server does). It is a client secret for a local port, stored
-- beside the database it belongs to, not a secret of this service's own — see §25.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_endpoints (
    kind          TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    token         TEXT,
    label         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_probe_at TEXT,
    last_error    TEXT
);

-- --------------------------------------------------------------------------
-- agenest: which conversations exist.
--
-- Separate tables rather than reusing `sessions`/`session_events`, because the two are
-- different shapes of thing. A dashboard session is a *report* about work happening
-- elsewhere (see docs/design.md §2 — its status is reported, never inferred). An agent
-- thread is a conversation, and this table holds what is *ours* about it: its name, which
-- agent it belongs to, and the agent's own id for it.
--
-- **The conversation itself is not here.** It belongs to the agent — Pi keeps a session file
-- and that file is what the page reads — and a second copy in this service would be a second
-- answer to "what was said", written by different code, drifting. An agent with no readable
-- transcript (nanobot) keeps its own storage inside its own adapter, schema and all. See
-- `backends/base.py` on `history()`.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_threads (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    -- Which agent this conversation is with. Persisted per thread rather than read from
    -- config on demand, so changing CSD_AGENT_BACKEND does not silently re-point old
    -- conversations at a different agent (and lose them).
    backend         TEXT NOT NULL DEFAULT 'nanobot',
    -- The backend's own id for the conversation: nanobot's `chat_id`, or for `native` the Pi
    -- session id, which is the same string as this thread's `--session-id`. A foreign
    -- reference, never our identity: it is minted by someone else's id generator, so a schema
    -- built on it could not be reasoned about or migrated locally.
    backend_conversation_id TEXT,
    provider        TEXT,
    model           TEXT,
    workdir         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_threads_updated
    ON agent_threads (updated_at DESC);

-- Event delivery looks up a thread by the backend's conversation id, on every incoming
-- frame, so it gets an index of its own.
CREATE INDEX IF NOT EXISTS idx_agent_threads_conversation
    ON agent_threads (backend_conversation_id);

-- --------------------------------------------------------------------------
-- Files carried by a conversation (docs/design.md §29).
--
-- Separate from `agent_threads` for the same reason the thread table is separate from
-- `sessions`: this is a record *we* keep, and it is the one part of the conversation the
-- agent's own transcript cannot carry. A channel whose protocol has no media frame moves
-- bytes through our disk and a path in the message text; the path is how the agent reads an
-- incoming file, and the outbox is how it hands one back.
--
-- `direction` is `in` (the user sent it) or `out` (the agent put it in the outbox). An
-- outgoing row is the message: it has no counterpart in the agent's transcript, so history
-- merges these rows into the transcript rather than decorating an existing entry.
--
-- `path` is absolute on this host. It is ours to serve; the agent is only ever told it.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_attachments (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE CASCADE,
    -- The backend's conversation id at delivery time. Nullable because an incoming file can
    -- be uploaded before the first message has created a conversation; outgoing rows always
    -- have one, because the outbox is per conversation.
    conversation_id TEXT,
    direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    name            TEXT NOT NULL,
    mime            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    path            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_attachments_thread
    ON agent_attachments (thread_id, created_at, id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Kept only so `close()` can reach connections created on other threads; sqlite3
        # connections are not garbage collected promptly enough to rely on for file handles.
        self._all: set[sqlite3.Connection] = set()
        self._all_lock = threading.Lock()
        conn = self._connection()
        conn.executescript(SCHEMA)
        self._migrate()

    def _connection(self) -> sqlite3.Connection:
        """This thread's connection, created on first use.

        One connection per thread rather than one per process: see the module docstring. It is
        also why `write()` can promise anything — two threads no longer share a transaction.

        `isolation_level=None` turns off sqlite3's implicit `BEGIN`, so `write()` decides when
        a transaction starts, which is what makes `BEGIN IMMEDIATE` possible.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            with self._all_lock:
                self._all.add(conn)
        return conn

    def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        `executescript(SCHEMA)` above is entirely `CREATE TABLE IF NOT EXISTS`, which is
        exactly right for tables and exactly wrong for columns: a table that already exists is
        left alone, so a column added to it never appears. A fresh dev database always has the
        new shape and this does nothing — which is the trap, because the *deployed* database is
        the one with the old shape, and there a missing column fails at query time rather than
        at boot.

        Additive and idempotent by construction: the column's own presence is the marker, so
        there is no bookkeeping row that can drift from reality. That is the difference from
        `agent/legacy.py`, which needs a marker precisely because it *drops* something. There
        is no down-migration: a leftover column is harmless, and deciding when to remove one is
        how a migration ends up deleting data it cannot put back.
        """
        columns = {
            row["name"] for row in self._connection().execute("PRAGMA table_info(sessions)")
        }
        if "host" not in columns:
            self._connection().execute("ALTER TABLE sessions ADD COLUMN host TEXT")

        # What a connector declares about being driven (§24). Defaulted rather than nullable,
        # so a row written by an older connector reads as "did not ask" instead of "unknown".
        self._add_missing(
            "agent_hosts",
            {
                "wants_access": "INTEGER NOT NULL DEFAULT 0",
                "ssh_host": "TEXT",
                "ssh_user": "TEXT",
                "ssh_port": "INTEGER NOT NULL DEFAULT 22",
            },
        )

    def _add_missing(self, table: str, columns: dict[str, str]) -> None:
        """`ALTER TABLE ... ADD COLUMN` for whichever of `columns` the table lacks.

        Each column's own presence is the marker, exactly like the `host` column above, so a
        second boot is a no-op and there is nothing to keep in sync.
        """
        existing = {
            row["name"] for row in self._connection().execute(f"PRAGMA table_info({table})")
        }
        for name, declaration in columns.items():
            if name not in existing:
                self._connection().execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @classmethod
    def open_read_only(cls, path: Path) -> "Database":
        """A connection that can only read, for the personal agent's registry tool.

        The tool runs in the agent's own process tree and must not be able to write the record
        — and it must not run `executescript(SCHEMA)`/`_migrate` on the way in either, which is
        why this skips both rather than reusing the normal constructor. WAL means a reader needs
        the `-wal`/`-shm` files readable, which they are: same user, same box.
        """
        instance = cls.__new__(cls)
        instance.path = path
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        # The instance has no `__init__`, so it has to carry the same per-thread scaffolding
        # the normal constructor builds — otherwise `query()` would lazily open a *read-write*
        # connection and the read-only promise would be gone.
        instance._local = threading.local()
        instance._local.conn = conn
        instance._all = {conn}
        instance._all_lock = threading.Lock()
        return instance

    def close(self) -> None:
        """Close every connection this object opened, on whichever thread it was made."""
        with self._all_lock:
            connections, self._all = self._all, set()
        for conn in connections:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A transaction that owns the write lock from its first statement.

        `BEGIN IMMEDIATE` before anything is read, so two writers queue at the lock instead of
        reading the same row and overwriting each other. Without it a `SELECT` inside the block
        sees a snapshot another writer is about to change, and the stale value it writes back
        is the lost update `docs/design.md` §35 records.
        """
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._connection().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._connection().execute(sql, params)


_db: Database | None = None


def init_database(settings: Settings) -> Database:
    global _db
    _db = Database(settings.db_path)
    return _db


def get_database() -> Database:
    if _db is None:
        raise RuntimeError("database is not initialised; call init_database() at startup")
    return _db
