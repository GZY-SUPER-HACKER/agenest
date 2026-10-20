"""agenest — this service as a client of an agent.

Not an agent. The product is a control plane that talks to one you already run: nanobot,
OpenClaw and Hermes are the adapters that exist, and a local Pi process is the fallback that
works without a gateway to install. `backends/README` is `design.md` §17.

Kept as its own package, and behind its own credential, because it is a different kind of
thing from the dashboard it lives beside. The dashboard *reports on* work happening elsewhere
and can do nothing (docs/design.md §1). This one takes instructions from a page and gives
them to an agent that can read and write files. Mixing the two under one auth model would mean
the read-only promise that makes the dashboard safe to expose could no longer be stated.
"""

from .backends import (
    AgentBackend,
    BackendError,
    BackendStatus,
    build_backend,
    default_backend,
)
from .rpc import PiCommandError, PiProcess, PiProtocolError, PiTimeout
from .supervisor import Supervisor

__all__ = [
    "AgentBackend",
    "BackendError",
    "BackendStatus",
    "PiCommandError",
    "PiProcess",
    "PiProtocolError",
    "PiTimeout",
    "Supervisor",
    "build_backend",
    "default_backend",
]
