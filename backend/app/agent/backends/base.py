"""The seam between "which agent" and everything else.

This service is a *client* of an agent, not an agent. That reframing is what this package
exists to encode: everything above it — the thread store, the SSE stream, the page — is
written against a conversation and a stream of normalized events, and knows nothing about
which agent produced them.

Two backends ship:

- **nanobot** (`backends/nanobot.py`) — the point of the exercise. Connects to a running
  [nanobot](https://github.com/HKUDS/nanobot) gateway over its documented websocket channel,
  which is exactly the interface nanobot provides for a rich external client. The agent, its
  memory, its model keys and its tools all live over there; this service only carries
  messages back and forth.
- **native** (`backends/native.py`) — a Pi process this service runs itself. Kept because a
  local agent with no gateway to install is genuinely useful, not as the main event.

Adding a third (openclaw and friends) means implementing this interface and nothing else.
The reason it is worth having a seam at all rather than special-casing nanobot: the two
backends differ in almost every mechanism — a websocket multiplexing many chats versus a
subprocess per conversation — while agreeing completely on what a conversation and an event
*are*. That is the line worth drawing.

Events are emitted **per conversation**, and the supervisor maps a conversation id onto one of
its own thread ids. External ids are never used as our primary keys: nanobot mints `chat_id`
values, and a schema whose identity depends on someone else's id generation cannot be
migrated or reasoned about locally.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ...schemas import AgentRole

# --------------------------------------------------------------------------- #
# normalized events
#
# Deliberately the same vocabulary the page already speaks. A backend's job is translation,
# not invention — if a backend cannot express something in these terms, that is a signal the
# abstraction is wrong, not a reason to add a backend-specific frame the UI must learn.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Delta:
    """A chunk of the reply still being written. Never stored — see `stream.AgentBroker`.

    `replace` is the one piece of streaming structure that is not an append. A backend that
    restarts an item — OpenClaw does, on item boundaries and media updates — cannot express
    "this is the whole text now" by appending, and a client told to append would render the
    answer twice. It is a *general* concept rather than a backend-specific frame, which is
    the test `base.py` sets for adding anything here: nanobot and native simply never set it.
    """

    channel: Literal["text", "thinking"]
    text: str
    replace: bool = False


@dataclass(slots=True)
class Message:
    """A completed unit of conversation.

    `id` is the **backend's own stable identifier** for this message, and it is what the client
    dedups on: the page renders a message optimistically from the POST response and then meets it
    again in a re-fetched history, and the two must be recognised as one. A backend that can name
    its own messages (Pi's session-entry id) supplies one; a backend that cannot leaves it `None`
    and the layer above assigns one.

    `args` is a tool call's arguments — for `bash`, the command line. It is on `Message` rather
    than only on `ToolCall` because `ToolCall` is a live frame and is never stored, so a tool's
    arguments used to be visible on screen and then gone: memory consolidation could see that a
    command failed but not which one. Anything that records a transcript needs both halves.

    `created_at` is the agent's own timestamp for the message, as an ISO 8601 string, or `None`
    when its protocol does not carry one. It belongs on this dataclass because the page renders
    it and groups by day: stamping it in the layer above would give every message the time the
    page happened to load, which is a timestamp that is wrong on every refresh. A backend that
    cannot read one leaves it `None`; the wire layer falls back to "now" only then.

    `progress` marks text the agent wrote *alongside* its tool calls rather than as an answer.
    It is a field on the message and **not a frame of its own**, which is the design decision
    worth stating: a checkpoint is text the agent recorded, so a backend that emitted it only as
    a live frame would lose it on the next refresh — for nanobot, whose record is this adapter's
    own table, that would be the whole feature. One shape also means the page has one rendering
    path, and the row dedups like any other message instead of needing a second identity scheme
    (docs/design.md §23).
    """

    role: AgentRole
    content: str
    id: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    args: dict | None = None
    is_error: bool = False
    created_at: str | None = None
    progress: bool = False


@dataclass(slots=True)
class ToolCall:
    """A tool starting or finishing.

    `start` carries no result because there isn't one yet: a tool that began and never
    finished has no outcome, and the page shows it as running rather than guessing.
    """

    phase: Literal["start", "end"]
    name: str | None = None
    call_id: str | None = None
    is_error: bool = False
    args: dict | None = None


@dataclass(slots=True)
class Status:
    """Anything that changes how the header should read."""

    streaming: bool | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(slots=True)
class Notice:
    """An aside worth surfacing and not worth recording: compacting, retrying, an error."""

    notice: str
    level: Literal["info", "error"] = "info"
    detail: str | None = None


BackendEvent = Delta | Message | ToolCall | Status | Notice

#: Called as `(conversation_id, event)`. Sync or async; the caller awaits if needed.
EventSink = Callable[[str, BackendEvent], Awaitable[None] | None]


@dataclass(slots=True)
class BackendStatus:
    """What the status endpoint can honestly say about a backend."""

    name: str
    connected: bool
    detail: str | None = None
    conversations: int = 0
    tools: list[str] = field(default_factory=list)
    #: What the agent is configured to run on. Optional because only a backend that runs the
    #: model itself — `native` — can answer; a gateway agent's model is its own business, and
    #: reporting one here would be this page guessing at another program's settings.
    provider: str | None = None
    model: str | None = None


class BackendError(RuntimeError):
    """The backend could not do what was asked, with a message worth showing a person."""


class BackendUnavailable(BackendError):
    """The agent is not there: no gateway listening, or the process would not start.

    A distinct type rather than a message the API greps for. "The agent is down" and "the
    agent refused this" are the two failures a user actually has to tell apart — the first
    means start nanobot, the second means read what it said — and matching on substrings to
    decide which HTTP status to send is the kind of thing that silently stops working the
    next time someone rewords an error.
    """


class AgentBackend(Protocol):
    """One agent, reachable by one mechanism.

    Conversations are addressed by an opaque `conversation_id` the *backend* mints, so that
    each can use whatever identity its protocol already has (nanobot's `chat_id`, or a local
    thread id for a process-per-conversation backend) without the layer above caring.
    """

    #: Stable identifier, persisted on each thread and shown in the UI.
    name: str

    async def start(self) -> None:
        """Bring the backend up. Must be safe to call on an idle service."""
        ...

    async def stop(self) -> None:
        """Shut down and release everything. Called before the database closes."""
        ...

    async def new_conversation(self) -> str:
        """Mint a fresh conversation on the agent and return its id."""
        ...

    async def attach(self, conversation_id: str) -> None:
        """Resume an existing conversation, so its replies reach us again.

        Called on reconnect as well as on load: a backend that multiplexes conversations
        needs to be told which ones this process cares about after the connection drops.
        """
        ...

    async def send(self, conversation_id: str, text: str) -> Message | None:
        """Deliver a user message. Returns when the agent has *accepted* it, not answered.

        Returns the message **as the agent recorded it**, which is to say carrying the agent's
        own id for it, or `None` when that id cannot be established.

        The return value is what lets the page draw the user's own message immediately and then
        recognise it again in a re-fetched history. Deriving the id here rather than minting one
        in the layer above is what keeps that free: an invented id would have to be reconciled
        against the real one later, forever.

        **Return `None` rather than a guess.** A backend that can see an id but cannot tell what
        it identifies — a response carrying both a run id and a message id, with nothing to say
        which is which — must answer `None`. An id that will never match the history is worse
        than no id: the page then holds a duplicate wearing a label that says it is not one.
        """
        ...

    async def history(self, conversation_id: str) -> list[Message]:
        """The conversation so far, oldest first.

        **The rule that makes this a seam rather than a table.** A backend whose agent keeps a
        transcript it can read — Pi writes a session file and answers `get_entries` — returns it
        here, and the conversation is then read from one place with no copy kept anywhere else.
        A backend whose agent keeps no readable transcript returns whatever it has had to record
        itself, which is that adapter's business and its own storage.

        What used to happen instead was a single `agent_messages` table in the core that every
        backend wrote to. That table's shape was the *lowest common denominator* across agents,
        which meant it was lossy for all of them: Pi's tool arguments, its thinking, and its
        per-message model all had nowhere to go. A shared schema for other people's formats is
        not a shared model; it is everyone's leftovers.

        Returning an empty list is honest for an agent that cannot replay (nanobot's `attach`
        subscribes without replaying, and its channel carries no history frame). The page then
        shows what this connection has seen, which is what an IM client can claim — and the fix
        for that belongs in that adapter, not in the core.
        """
        ...

    async def abort(self, conversation_id: str) -> bool:
        """Stop the current run. False when there was nothing running to stop."""
        ...

    def is_live(self, conversation_id: str) -> bool:
        """Whether this conversation currently has something behind it."""
        ...

    def is_streaming(self, conversation_id: str) -> bool:
        """Whether a reply is arriving right now."""
        ...

    def is_available(self) -> bool:
        """Whether this backend could be used *at all*, ignoring current connectivity.

        Distinct from `status().connected`, and the distinction is what makes the settings
        panel useful. A gateway that is merely down is available — start it and it works. A
        local agent whose binary is not installed is not available, and telling someone to
        "reconnect" when the fix is "install it" sends them the wrong way.

        Must not raise and must not block: it is called while rendering a list.
        """
        ...

    def status(self) -> BackendStatus:
        ...


__all__ = [
    "AgentBackend",
    "BackendError",
    "BackendEvent",
    "BackendStatus",
    "BackendUnavailable",
    "Delta",
    "EventSink",
    "Message",
    "Notice",
    "Status",
    "ToolCall",
]
