"""Agent backends, and the registry that finds them.

`build_backend` is the only code in the service that knows more than one implementation
exists. The supervisor, the store, the API and the page all address `AgentBackend`, which is
what makes "which agent" a configuration value rather than an architectural fork.

**Adding an agent is additive, not a change.** Write `backends/<name>.py` implementing
`base.AgentBackend`, define `BACKEND_NAME`, and restart — the registry scans this package,
the settings panel picks the new row up, and nothing in the core learns the agent's name.
That is the whole point of the seam: nanobot, OpenClaw and Hermes disagree about nearly
every mechanism (one socket multiplexing chats, a JSON-RPC method surface, a per-request
auth ticket) while agreeing completely on what a conversation and an event *are*.
"""

from __future__ import annotations

from ...config import Settings
from .base import (
    AgentBackend,
    BackendError,
    BackendEvent,
    BackendStatus,
    BackendUnavailable,
    Delta,
    EventSink,
    Message,
    Notice,
    Status,
    ToolCall,
)
from ..registry import (
    available,
    build_backend,
    default_backend,
    describe,
    discover,
    ensure_registered,
    has,
    is_offered,
)

#: The interface every backend implements, and the vocabulary it emits.
Backend = AgentBackend

__all__ = [
    "AgentBackend",
    "Backend",
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
    "available",
    "build_backend",
    "default_backend",
    "describe",
    "discover",
    "ensure_registered",
    "has",
    "is_offered",
]
