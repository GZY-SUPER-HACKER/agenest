"""Pydantic request/response schemas.

The vocabulary of the contract lives here once:
- valid statuses and event types are closed sets (spec 4.2, 4.3)
- display strings carry length caps (spec 9)
- times are UTC, emitted as ISO-8601 with `Z`; `received_at` is the only key the
  server orders by, `occurred_at` is whatever the reporter's clock said (spec 4.3)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ApiModel(BaseModel):
    """Reject unknown fields: a typo in `status` must not silently mean `running`."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 4.2 statuses
# --------------------------------------------------------------------------- #
class SessionStatus(str, Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"
    unknown = "unknown"


#: A late event or patch must never move a session *out* of these (spec 6.4, §12).
TERMINAL_STATUSES = frozenset(
    {SessionStatus.completed, SessionStatus.failed, SessionStatus.interrupted}
)


# --------------------------------------------------------------------------- #
# 4.3 event types
# --------------------------------------------------------------------------- #
class EventType(str, Enum):
    session_started = "session_started"
    status_changed = "status_changed"
    progress = "progress"
    note = "note"
    #: The agent says it is blocked on a human — a permission prompt, a question with
    #: options, anything that will not proceed until someone answers. Reported, never
    #: inferred, and deliberately **not** a status: it describes what the session needs
    #: right now, while `status` describes how the session ends. Marking it terminal would
    #: make it sticky, and a session that is merely waiting would never come back.
    awaiting_user = "awaiting_user"
    #: The agent finished *a reply*, and the session is still open. Claude Code's `Stop` hook
    #: fires here, once per answer, while the process may live for days — so this is a turn
    #: boundary, never a lifecycle signal (docs/design.md §15, §36). The UI reads it to render
    #: `idle` beside the session's reported `running`; it moves no status.
    turn_completed = "turn_completed"
    #: A sentence the agent itself wrote while working — the middle ground between one line
    #: per turn and one row per tool call. `payload.source` names where it came from
    #: (`prose` today, `plan`/`step` for reporters that can supply them), so a checkpoint is
    #: never mistaken for a summary the dashboard composed (docs/design.md §37). It is a
    #: display event and moves no status, exactly like `awaiting_user` / `turn_completed`.
    checkpoint = "checkpoint"
    session_completed = "session_completed"
    session_failed = "session_failed"
    session_interrupted = "session_interrupted"
    heartbeat = "heartbeat"


#: Event types that carry a terminal status. Reporting one of these closes the session.
TERMINAL_EVENTS: dict[EventType, SessionStatus] = {
    EventType.session_completed: SessionStatus.completed,
    EventType.session_failed: SessionStatus.failed,
    EventType.session_interrupted: SessionStatus.interrupted,
}

#: Reverse map, for synthesising the event that records an explicit status patch.
TERMINAL_STATUS_TO_EVENT: dict[SessionStatus, EventType] = {
    SessionStatus.completed: EventType.session_completed,
    SessionStatus.failed: EventType.session_failed,
    SessionStatus.interrupted: EventType.session_interrupted,
}

#: Events that only describe ongoing work; they must not resurrect a terminal session.
NON_TERMINAL_EVENTS = frozenset(
    {
        EventType.session_started,
        EventType.status_changed,
        EventType.progress,
        EventType.note,
        EventType.awaiting_user,
        EventType.turn_completed,
        EventType.checkpoint,
        EventType.heartbeat,
    }
)

#: Event types that should not appear as a session "last activity" line.
#: `heartbeat` is a liveness signal, not a thing the user did.
NON_DISPLAY_EVENTS = frozenset({EventType.heartbeat})


# --------------------------------------------------------------------------- #
# length caps (spec 9)
# --------------------------------------------------------------------------- #
MAX_TITLE = 200
MAX_PROJECT = 120
MAX_WORKSPACE = 500
MAX_MESSAGE = 500
MAX_SUMMARY = 2000
MAX_AGENT_TYPE = 64
MAX_HOST = 64
#: An OS user name. Same cap as a hostname, and separate because the two are validated in
#: different places (§24) — collapsing them would make a change to one silently move the other.
MAX_SSH_USER = 64
MAX_EVENT_KEY = 128
MAX_PAYLOAD_KEYS = 64


class _CappedStr(str):
    """Marker only; caps are enforced by Field(max_length=...)."""


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
class SessionCreate(ApiModel):
    agent_type: str = Field(max_length=MAX_AGENT_TYPE)
    title: str = Field(max_length=MAX_TITLE)
    project_name: str | None = Field(default=None, max_length=MAX_PROJECT)
    workspace: str | None = Field(default=None, max_length=MAX_WORKSPACE)
    #: Which machine the work ran on — `csd-agent`'s label, i.e. the agent host's own name for
    #: itself. Optional because a reporter that has not been told a host must not invent one;
    #: rows without it are grouped separately rather than attributed to a guess.
    host: str | None = Field(default=None, max_length=MAX_HOST)
    status: SessionStatus = SessionStatus.running
    started_at: str | None = None

    @field_validator("agent_type", "title", "project_name", "workspace", "host")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("title")
    @classmethod
    def _title_required(cls, v: str) -> str:
        if not v:
            raise ValueError("title must not be blank")
        return v

    @field_validator("started_at")
    @classmethod
    def _ts(cls, v: str | None) -> str | None:
        if v is not None:
            parse_any_timestamp(v)
        return v


class SessionPatch(ApiModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    project_name: str | None = Field(default=None, max_length=MAX_PROJECT)
    status: SessionStatus | None = None
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY)
    exit_code: int | None = None

    @field_validator("title", "project_name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("exit_code")
    @classmethod
    def _exit_code_range(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if not -128 <= v <= 255:
            raise ValueError("exit_code must be within a process exit-code range")
        return v


class EventCreate(ApiModel):
    event_type: EventType
    message: str = Field(max_length=MAX_MESSAGE)
    payload: dict[str, Any] | None = None
    #: Idempotency key. The reporter supplies it so a retried POST inserts at most one row.
    event_key: str | None = Field(default=None, max_length=MAX_EVENT_KEY)
    occurred_at: str | None = None

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be blank")
        return v

    @field_validator("payload")
    @classmethod
    def _payload(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return None
        if len(v) > MAX_PAYLOAD_KEYS:
            raise ValueError(f"payload must have at most {MAX_PAYLOAD_KEYS} keys")
        for key, value in v.items():
            if len(key) > MAX_EVENT_KEY:
                raise ValueError("payload key too long")
            if not isinstance(value, (str, int, float, bool, type(None), list)):
                raise ValueError("payload values must be JSON scalars or arrays")
            if isinstance(value, list) and len(value) > 100:
                raise ValueError("payload array too long")
            if isinstance(value, str):
                capped = cap_string(value)
        return v


def cap_string(value: str, limit: int = MAX_SUMMARY) -> str:
    """Truncate over-long payload strings instead of rejecting the whole event.

    A `progress` event must never be dropped because a reporter stuffed a stack trace in it.
    """
    if len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def parse_any_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #
class EventOut(ApiModel):
    id: int
    session_id: str
    event_type: EventType
    message: str
    payload: dict[str, Any] | None = None
    occurred_at: str
    received_at: str


class SessionOut(ApiModel):
    id: str
    agent_type: str
    title: str
    project_name: str | None = None
    workspace: str | None = None
    #: The machine this ran on, as the reporter named it. `None` on rows reported before hosts
    #: existed, and on any reporter that does not know — a client groups those together rather
    #: than guessing which box they belong to.
    host: str | None = None
    status: SessionStatus
    started_at: str
    last_activity_at: str
    finished_at: str | None = None
    summary: str | None = None
    exit_code: int | None = None
    created_at: str
    updated_at: str
    #: True when a running session has gone quiet past the stale threshold. Display-only
    #: hint; the stored status is never rewritten to reflect it (spec 4.2).
    possibly_stale: bool = False
    #: The newest non-heartbeat event. Present so a list view can say what a session is
    #: *doing* without fetching each one — the sidebar groups by harness, and a group of
    #: rows that only said "running" would tell the user nothing. `heartbeat` is excluded:
    #: a liveness ping is not something the user did.
    last_event: "EventOut | None" = None


class SessionDetailOut(SessionOut):
    heartbeat_count: int = 0
    events: list[EventOut] = []


class SessionListOut(ApiModel):
    sessions: list[SessionOut]
    total: int
    limit: int
    offset: int


class EventListOut(ApiModel):
    events: list[EventOut]
    total: int


class SessionCreated(ApiModel):
    session: SessionOut
    event: EventOut


class EventAccepted(ApiModel):
    accepted: Literal[True] = True
    session_id: str
    event: EventOut


# --------------------------------------------------------------------------- #
# hosts — the machines that run coding agents
#
# A host is **not** a session, and modelling it as one was rejected: a session is work being
# reported, and its status describes how that work ended. A host is a machine being
# *reachable*, which is a property that has no outcome and never finishes. Forcing it into
# `sessions` would put rows in the list that are not work, and give `completed`/`failed` a
# meaning they do not have. See docs/design.md §20.
#
# The identity is `(label, agent_type)` and not `label` alone, because the thing that reports
# is a *connector for one coding agent*: a box running both Claude Code and codex connects
# twice, through two connectors, and those are two facts. Keying on the label would make the
# second connection look like a refresh of the first.
# --------------------------------------------------------------------------- #
class HostReport(ApiModel):
    """What a connector says about itself.

    The first three fields are all a connector *has* to say. The rest are what it declares
    about being driven (docs/design.md §24): that the user may authorize the personal agent to
    ssh into this machine, and where the connector believes it can be reached. Declaring is not
    authorizing — `host_access` is the table whose row only a person can create — so the worst a
    forged declaration achieves is a prompt the user is free to ignore.
    """

    label: str = Field(max_length=MAX_HOST)
    agent_type: str = Field(max_length=MAX_AGENT_TYPE)
    #: `False` on uninstall, or when the connector is winding down. Reported, never inferred:
    #: a host that simply stops talking stays `connected` and goes `possibly_stale`, which is
    #: the honest rendering of "we do not know".
    connected: bool = True
    #: "I can be driven." Advisory, and true by default on a fresh `csd-agent install`.
    wants_access: bool = False
    #: The address the *connector* thinks reaches it — a fqdn, a LAN address, a Tailscale IP.
    #: Unverified by the dashboard: nothing here can tell whether it is reachable, which is
    #: why the approved address is stored separately and shown to the user before use.
    ssh_host: str | None = Field(default=None, max_length=MAX_HOST)
    #: The OS user the connector runs as. It is the only account it can install a key into,
    #: which is why the UI treats this as read-only.
    ssh_user: str | None = Field(default=None, max_length=MAX_SSH_USER)
    ssh_port: int = Field(default=22, ge=1, le=65535)

    @field_validator("label", "agent_type")
    @classmethod
    def _required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("ssh_host", "ssh_user")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        """An empty string is "not declared", not a value: a connector that has no address to
        report must not leave a blank that reads like one."""
        if v is None:
            return None
        v = v.strip()
        return v or None


#: What the personal agent may do with a registered host (§24). Derived per read from the
#: connector's declaration and the user's approval; never stored, because two of the six are
#: comparisons and a stored copy of a comparison is a second answer waiting to drift.
#: `None` on `HostOut` means the connector never asked to be driven.
AccessState = Literal["requested", "pending", "ready", "address_changed", "disabled"]


class HostOut(ApiModel):
    id: str
    label: str
    agent_type: str
    connected_at: str
    last_seen_at: str
    disconnected_at: str | None = None
    #: Same display hint, same threshold, same rendering rule as on a session: an inferred
    #: state must never look like a reported one.
    possibly_stale: bool = False
    #: What the connector declared, and what the user approved. `access_target` is the
    #: approved address only — the declared one is visible through the host page rather than
    #: here, so the UI cannot accidentally offer an address nobody checked.
    wants_access: bool = False
    access_state: AccessState | None = None
    access_target: str | None = None
    access_fingerprint: str | None = None


class HostAccessRequest(ApiModel):
    """A person authorizing (or re-pointing) access to one host.

    Every field optional: the defaults are what the connector declared, which is what the
    common case wants. Overriding the user is possible but warned about in the UI, because the
    key still lands in the *connector's* account.
    """

    ssh_host: str | None = Field(default=None, max_length=MAX_HOST)
    ssh_user: str | None = Field(default=None, max_length=MAX_SSH_USER)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)


class HostAccessOut(ApiModel):
    """The result of authorizing. The private key is never part of any response."""

    host: HostOut
    public_key: str | None = None
    fingerprint: str | None = None
    #: What happens next, in the words the UI shows: the host installs the key itself on its
    #: next heartbeat, so there is a waiting state and it should be named rather than implied.
    note: str | None = None


class HostAccessFetchOut(ApiModel):
    """What a connector is told when it asks about its own access.

    Deliberately without `key_path`: a connector has no use for where the dashboard keeps its
    private half, and a field that is never sent cannot be logged by accident.
    """

    state: str
    public_key: str | None = None
    comment: str | None = None


class HostAccessReport(ApiModel):
    """A connector saying whether it managed to install the key."""

    label: str = Field(max_length=MAX_HOST)
    agent_type: str = Field(max_length=MAX_AGENT_TYPE)
    installed: bool = True
    #: Why not, when it could not — an unwritable `authorized_keys`, a read-only home. Shown
    #: to the user instead of leaving the row at `pending` forever with no explanation.
    error: str | None = Field(default=None, max_length=MAX_MESSAGE)


class HostListOut(ApiModel):
    hosts: list[HostOut]


# --------------------------------------------------------------------------- #
# agenest — the personal agent's contract
#
# Deliberately *not* expressed in terms of `SessionStatus`/`EventType` above. Those describe
# a report about work happening on another machine; these describe a conversation with an
# agent this service is itself running. Reusing the vocabulary would make "status" mean two
# unrelated things in one codebase.
# --------------------------------------------------------------------------- #
class AgentRole(str, Enum):
    user = "user"
    assistant = "assistant"
    tool = "tool"


class AgentMessageCreate(ApiModel):
    """A prompt, optionally carrying files.

    `content` may be blank **only when there is at least one attachment** — sending a photo
    with no caption is a message, and refusing it would make the + button useless on its own.
    The cross-field rule is stated here rather than in the route so a malformed body is a 422
    with the field named, the same as any other validation failure.
    """

    content: str = Field(default="", max_length=MAX_SUMMARY)
    #: Ids returned by the upload route, in the order the user attached them. Ten is a message
    #: with an album in it; beyond that the page is a file manager, which this is not.
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("content")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _not_blank(self) -> "AgentMessageCreate":
        if not self.content.strip() and not self.attachment_ids:
            raise ValueError("content or attachment_ids must be provided")
        return self


class AgentAttachmentOut(ApiModel):
    """A file carried by the conversation, in one direction.

    The bytes are served by `GET /api/agent/attachments/{id}`; this model is the metadata the
    transcript needs to draw a card. `direction` is on the wire because the page draws an
    incoming file inside the user's bubble and an outgoing one as the agent's own — the same
    distinction the message roles already carry, made explicit for a card that may stand
    alone with no text around it.
    """

    id: str
    name: str
    mime: str
    size: int
    direction: Literal["in", "out"] = "in"
    created_at: str | None = None


class AgentMessageOut(ApiModel):
    """One message, carrying **the backend's own id** for it.

    A `str`, not a row id, because the identity belongs to the agent rather than to a table of
    ours: Pi's session-entry id is what a client must dedup on, and it has to be the same value
    across a refresh, a reconnect and a service restart. An id minted here would satisfy none of
    those. It is `None` only when a backend cannot name its own messages.

    `args` is a tool call's arguments — the command line, for `bash`. It travels with the tool
    row so that what was *run* and what came back are one fact rather than two, which is what
    lets a re-read conversation (and the memory consolidator) know both.

    `progress` marks a checkpoint rather than an answer: text the agent wrote in the same entry
    as its tool calls. It is a field on the message rather than only a live frame because the
    text is *in the agent's record* — a re-read conversation returns it too, and the same words
    must not render as a reply after a refresh and a checkpoint before one.
    """

    id: str | None = None
    thread_id: str
    role: AgentRole
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    args: dict[str, Any] | None = None
    is_error: bool = False
    created_at: str
    progress: bool = False
    #: Files delivered with this message (docs/design.md §29). The message text carries a
    #: marker line naming each file's path — that is what the agent reads — and this list is
    #: the resolved half the page draws. The marker is stripped from `content` on the way out,
    #: so the page never has to parse it and a client that ignores this field sees clean text.
    attachments: list[AgentAttachmentOut] = []


class AgentThreadOut(ApiModel):
    id: str
    title: str
    #: Which agent this conversation is with: `nanobot` or `native`. Stored per thread so
    #: changing the configured backend never re-points an existing conversation elsewhere.
    backend: str = "nanobot"
    #: The backend's own id for this conversation (nanobot's `chat_id`). Null until the
    #: conversation has actually been opened on the agent — which is why nothing is created
    #: until the first prompt.
    conversation_id: str | None = None
    provider: str | None = None
    model: str | None = None
    workdir: str | None = None
    created_at: str
    updated_at: str
    #: Whether the agent currently has something behind this conversation. False is not an
    #: error: a fresh thread is legitimately false until someone speaks to it.
    running: bool = False
    streaming: bool = False


class AgentThreadDetailOut(AgentThreadOut):
    messages: list[AgentMessageOut] = []


class AgentBackendOut(ApiModel):
    """One agent this deployment can talk to, and whether it would work right now.

    `ready` and `connected` are deliberately separate. A gateway that is merely down is
    *ready* — start it and it works. A local agent whose binary was never installed is not
    ready, and the fix is to install it. Collapsing the two would send someone to "reconnect"
    when they need to install something.
    """

    name: str
    active: bool = False
    ready: bool = True
    connected: bool = False
    detail: str | None = None
    #: Whether this backend's door was registered by the agent itself (§25). Separate from
    #: `ready` because the two answer different questions — "is this offered at all" versus
    #: "has something told us where to knock" — and the settings panel offers a forget action
    #: only for the ones the agent put there.
    registered: bool = False
    #: Whether the settings panel may offer a gear on this row (§26). True only for a backend
    #: this service runs itself, because only that one's model and credentials are ours.
    configurable: bool = False


class AgentEndpointIn(ApiModel):
    """What a personal agent posts when it says where it can be reached.

    Deliberately tiny: `kind` and `url` are the whole thing, and the other two are the agent
    being polite about itself. There is no `enabled` flag, no retry policy and no priority —
    a door is open or it is not, and the switch is the user's decision (§25).
    """

    kind: str = Field(min_length=1, max_length=64)
    #: Must be loopback. Validated in `agent/endpoints.py`, where the rule and its reason live.
    url: str = Field(min_length=1, max_length=500)
    #: The agent's own credential for that door, when it has one.
    token: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=64)


class AgentEndpointOut(ApiModel):
    """What we know about a registered door. **Never carries the token back out.**"""

    kind: str
    url: str
    label: str | None = None
    #: Whether a token is on file — the fact the caller needs, without the secret.
    has_token: bool = False
    created_at: str
    updated_at: str
    last_probe_at: str | None = None
    #: Empty when the last knock was answered. A string here means the door did not open.
    last_error: str | None = None


class AgentEndpointListOut(ApiModel):
    endpoints: list[AgentEndpointOut]


class AgentStatusOut(ApiModel):
    """What the agent page needs to render its header.

    There is deliberately no `enabled` field. The signal for "acting is switched off on this
    server" is a **403** from `auth.access_header` — which happens when no access token is
    configured at all — and the signal for "I need your token" is a **401** — a token is
    configured and this browser's is wrong. Two states the UI must tell apart anyway, since
    one is a configuration choice and the other is a missing credential. A boolean in a body
    that could only ever be fetched when it was already true would be noise pretending to be
    information.
    """

    #: The active backend's name — `native` or `nanobot`.
    backend: str
    #: Whether the active backend is reachable *right now*.
    connected: bool = False
    detail: str | None = None
    provider: str | None = None
    model: str | None = None
    workdir: str | None = None
    tools: list[str] = []
    threads_running: int = 0
    #: Every backend this deployment offers, for the settings panel.
    backends: list[AgentBackendOut] = []


class AgentSwitchOut(AgentStatusOut):
    """The switch's answer: the new state **and** the conversation the page should now show.

    One request instead of three. The page used to switch, then ask for the session, then ask
    for its messages — three sequential round trips, and over a phone link each one costs about
    a second, so a tap read as "the messages are not syncing". The three calls stay for API
    clients; this one is the page's tap.
    """

    thread: AgentThreadDetailOut


class AgentBackendSwitch(ApiModel):
    backend: str = Field(max_length=32)


# --------------------------------------------------------------------------- #
# the local agent's runtime configuration (docs/design.md §26)
# --------------------------------------------------------------------------- #
class NativeConfigPatch(ApiModel):
    """A partial update. **Omitted means unchanged; `null` means clear.**

    The distinction is the whole shape of this model: clearing a field is how you go back to the
    environment variable, and a client that had to send every field to change one would erase
    the rest by omission. `as_patch()` is what preserves it — Pydantic drops unset fields from
    `model_fields_set`, and that set is the patch.
    """

    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    #: Write-only. It is accepted here and never returned by any route.
    api_key: str | None = Field(default=None, max_length=500)
    api_kind: str | None = Field(default=None, max_length=40)
    context_window: int | None = None
    max_tokens: int | None = None
    tools: list[str] | None = Field(default=None, max_length=16)
    trust_project_files: bool | None = None

    def as_patch(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set}


class NativeEnvDefaultsOut(ApiModel):
    """What the environment would supply where the stored configuration is silent."""

    provider: str | None = None
    model: str | None = None
    tools: list[str] | None = None


class NativeEffectiveOut(ApiModel):
    """What the next Pi process will actually be started with."""

    provider: str | None = None
    model: str | None = None
    tools: list[str] = []


class NativeConfigOut(ApiModel):
    """The local agent's configuration. **It has no API key field, deliberately.**

    `api_key_set` says a key is stored and `api_key_env` names (never shows) the provider
    variables present in this service's environment. That is the same rule `agent_endpoints`
    follows as `has_token`: a value that can be read back is a value that leaks through every
    proxy log and screenshot on the path.
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_kind: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None
    trust_project_files: bool = False
    api_key_set: bool = False
    api_key_env: list[str] = []
    #: Read-only in the panel: it decides where the agent may write *and* where its memory
    #: lives, so moving it is an environment decision rather than a toggle (§26).
    workspace: str | None = None
    env_defaults: NativeEnvDefaultsOut = Field(default_factory=NativeEnvDefaultsOut)
    effective: NativeEffectiveOut = Field(default_factory=NativeEffectiveOut)


class NativeConfigCheckOut(ApiModel):
    """What a "check" could establish: that Pi resolves the provider and model.

    Not that the credential works — that needs a real request, which costs tokens and is what
    the first message already does. Saying so is the point of `detail`.
    """

    ok: bool
    detail: str
    provider: str | None = None
    model: str | None = None


# --------------------------------------------------------------------------- #
# the agent's memory (docs/design.md §18)
# --------------------------------------------------------------------------- #
class AgentMemoryFileOut(ApiModel):
    """One memory file, and whether it has anything in it.

    The content is **not** included. These files are the agent's working memory and can hold
    anything the user ever told it; a status endpoint is not where that gets shipped to a phone
    that merely asked whether memory is on. The count of entries is what the page needs to decide
    whether to offer a link.
    """

    name: str
    #: Whether the file exists on disk. A file that exists and is empty is a file Dream has
    #: created but not yet filled — different from one it has never touched.
    present: bool = False
    chars: int = 0


class AgentMemoryCommitOut(ApiModel):
    """One entry in the memory's own history.

    `subject` is derived from the file diff rather than from anything the model said, so it is a
    statement about what changed on disk. See §18: the model that edited the files is not a
    reliable witness to what it edited.
    """

    sha: str
    date: str
    subject: str


class AgentMemoryOut(ApiModel):
    """What the memory page renders.

    Deliberately no `enabled: bool`. Whether memory is on is answered by this route answering at
    all — the same reasoning as `AgentStatusOut`'s missing `enabled` field. A deployment without
    a memory directory has nothing to report, and inventing a boolean would mean this route had
    to answer in a state where it has no facts.
    """

    #: Where the files live, so the page can say where to look on disk.
    directory: str
    #: Journal entries awaiting consolidation. The one number that says whether the slow layer
    #: is keeping up — a value that only grows means Dream is not running.
    pending_entries: int = 0
    #: Total entries in the journal, consumed and not.
    journal_entries: int = 0
    #: Every file Dream may write, with its size.
    files: list[AgentMemoryFileOut] = []
    #: Recent memory commits, newest first. Empty when git is unavailable — which is a fact
    #: about the deployment, not an error.
    history: list[AgentMemoryCommitOut] = []
    #: Hours between automatic Dream runs; `0` means it never runs on its own.
    interval_hours: float = 2.0


class AgentDreamOut(ApiModel):
    """The outcome of one Dream run.

    `advanced` and `changed` are separate fields because they are separate facts, and §18 gives
    the cursor rule that depends on the difference: a run that completes without changing
    anything *does* advance, and reporting that as "nothing happened" would hide the only signal
    that distinguishes it from a run that failed.
    """

    ran: bool
    changed: bool = False
    advanced: bool = False
    detail: str = ""
    commit: str | None = None
