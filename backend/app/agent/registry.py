"""The agent backend registry.

One interface (`base.AgentBackend`), many agents. Everything above this package — the
supervisor, the store, the API, the page — addresses `AgentBackend` and never learns which
agent produced an event, so "which agent" is a configuration value rather than a fork.

**Why this is a registry and not the `if/else` chain that used to live here.** With two
backends, `build_backend` could ask `resolved == "nanobot"` and be done. That stops being
true at three, and the cost of the chain is paid every time: a new agent meant editing the
one file that also knows about every other agent. Now a backend is a module in this
directory that registers itself, and `build_backend` is a lookup.

Registration is by **module scan, not by an explicit list**: anything defining
`BACKEND_NAME` is offered. That is what makes adding an agent an additive change — drop in
`backends/openclaw.py`, restart, and the settings panel has a new row. Nothing in the core
names the new agent, which is the property worth having.

The one place that still knows every backend exists is the settings panel, and it learns
them from here rather than from a hard-coded tuple.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable, Iterator

from ..config import Settings
from . import backends as _backends_pkg
from .backends.base import AgentBackend, BackendEvent, EventSink

logger = logging.getLogger("csd.agent.registry")

#: Module attribute that marks an agent backend. Absence means "not a backend module" —
#: `base.py` and `__init__.py` are the only ones expected to lack it.
NAME_ATTR = "BACKEND_NAME"

#: Human-readable name shown in the settings panel. Falls back to `BACKEND_NAME`.
LABEL_ATTR = "BACKEND_LABEL"

#: One line on what connecting involves, shown under the backend's row. The point of this
#: field is to say *what the user has to do*, which differs a lot between agents: nanobot
#: needs nothing by default, OpenClaw needs a device pairing approval, Hermes needs a
#: same-OS-user control socket. A backend that needs no setup says so.
BLURB_ATTR = "BACKEND_BLURB"

#: How to open *this agent's* door, for the onboarding document a personal agent reads
#: (docs/design.md §25). A dict rather than prose so `agent/onboard.py` can render it as
#: Markdown for a model and as JSON for a client, from one declaration. Optional: a backend
#: that cannot be reached by an agent opening a door simply does not declare one.
ONBOARD_ATTR = "BACKEND_ONBOARD"

#: Whether the settings panel offers this backend. A module sets `BACKEND_OFFERED = False`
#: when its door is retired: the code stays registered — it still translates a real protocol,
#: and an explicit `CSD_AGENT_BACKEND=<name>` remains the operator's way in — but the page
#: stops advertising it and `POST /api/agent/backend` refuses it. A hidden-but-selectable row
#: would be worse than either showing it or removing it, because the choice would still exist
#: and nothing on screen would say so.
OFFERED_ATTR = "BACKEND_OFFERED"

#: Factories, in the order they are offered. Populated by `discover()`.
_FACTORIES: dict[str, Callable[[Settings, EventSink], AgentBackend]] = {}

#: The subset of `_FACTORIES` the page offers, so "registered" and "shown" can differ by
#: declaration rather than by a list in the core.
_OFFERED: set[str] = set()


def _iter_backend_modules() -> Iterator[str]:
    for info in pkgutil.iter_modules(_backends_pkg.__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        yield info.name


def discover() -> dict[str, Callable[[Settings, EventSink], AgentBackend]]:
    """Find every backend module in this package and read its registration.

    A module that imports but does not register is logged and skipped, not fatal: a broken
    third-party module must not take down the agent page. The same rule `Supervisor.__init__`
    applies to construction.
    """
    found: dict[str, Callable[[Settings, EventSink], AgentBackend]] = {}
    offered: set[str] = set()
    for name in _iter_backend_modules():
        try:
            module = importlib.import_module(f"{_backends_pkg.__name__}.{name}")
        except Exception:
            logger.exception("could not import the %s backend module; skipping it", name)
            continue

        factory = getattr(module, "build", None)
        backend_name = getattr(module, NAME_ATTR, None)
        if factory is None or not backend_name:
            logger.debug("module %r does not register a backend", name)
            continue

        found[backend_name] = factory
        if getattr(module, OFFERED_ATTR, True) is False:
            logger.debug("backend %r is registered but no longer offered", backend_name)
        else:
            offered.add(backend_name)
        logger.debug("registered agent backend %r from %s.py", backend_name, name)
    _OFFERED.clear()
    _OFFERED.update(offered)
    return found


def ensure_registered() -> dict[str, Callable[[Settings, EventSink], AgentBackend]]:
    """The registry, populated on first use and reused.

    Populated lazily rather than at import time so that importing this package stays cheap
    and side-effect free — `test_agent_contract.py` builds backends directly and must not
    pay for scanning, and a settings-only process has no reason to import a websocket
    client library either.
    """
    if not _FACTORIES:
        _FACTORIES.update(discover())
    return _FACTORIES


def describe(name: str) -> dict:
    """What the settings panel knows about a backend without constructing it.

    Label and blurb come from the module that registered it. A missing entry means a
    backend was removed or renamed; the panel says so rather than showing an empty row.
    """
    if name not in ensure_registered():
        return {"label": name, "blurb": None, "present": False}
    module = importlib.import_module(
        f"{_backends_pkg.__name__}.{_module_for(name)}"
    )
    return {
        "label": getattr(module, LABEL_ATTR, name),
        "blurb": getattr(module, BLURB_ATTR, None),
        "present": True,
    }


def _module_for(backend_name: str) -> str:
    """The module that registered a backend.

    Only needed for `describe()`, and only after `has()` has confirmed the name — so a
    miss here is a bug in a backend module, not something to handle gracefully.
    """
    for name in _iter_backend_modules():
        try:
            module = importlib.import_module(f"{_backends_pkg.__name__}.{name}")
        except Exception:
            continue
        if getattr(module, NAME_ATTR, None) == backend_name:
            return name
    raise KeyError(f"backend {backend_name!r} is registered but its module cannot be found")


def has(backend_name: str) -> bool:
    return backend_name in ensure_registered()


#: What a fresh deployment uses. Kept here rather than in `Settings` so that "which agents
#: exist" and "which one is the default" live in the same place: a backend author does not
#: have to know about either to add theirs.
#:
#: **native**, so the Agent tab works on a machine with nothing installed but Pi and a key.
#: The point of the product is still to be a channel onto an agent you already run —
#: nanobot, OpenClaw and Hermes are one tap away in settings, and switching is what the
#: settings panel is for. But a page that is dead on first open, because it defaults to a
#: gateway the user has never heard of, is a worse first impression than one that just
#: works locally.
DEFAULT_BACKEND = "native"


def default_backend() -> str:
    """The backend a fresh deployment talks to.

    A name, not a resolved instance: callers that need the object go through
    `build_backend`, because answering this should never construct anything.
    """
    return DEFAULT_BACKEND


def build_backend(
    settings: Settings, on_event: EventSink, *, name: str | None = None
) -> AgentBackend:
    """Construct one backend by name, defaulting to the configured one.

    An unknown name raises rather than silently falling back: someone who typed
    `CSD_AGENT_BACKEND=nanbot` wants nanobot, and quietly giving them the local agent instead
    would have them debugging the wrong machine. (`Supervisor` handles that case itself, at
    startup, where it can report it and still come up.)
    """
    factories = ensure_registered()
    resolved = (name or settings.agent_backend or default_backend()).strip().lower()
    factory = factories.get(resolved)
    if factory is None:
        known = ", ".join(sorted(factories)) or "none"
        raise ValueError(f"unknown agent backend {resolved!r}; expected one of {known}")
    return factory(settings, on_event)


def available() -> list[str]:
    """Every registered backend name, whether or not the page offers it.

    `Supervisor` builds these, because an explicit `CSD_AGENT_BACKEND` may name a retired one;
    the settings panel uses `offered()` instead.
    """
    return sorted(ensure_registered())


def offered() -> list[str]:
    """The registered backends the settings panel offers, in the same stable order."""
    ensure_registered()
    return sorted(_OFFERED)


def is_offered(backend_name: str) -> bool:
    """Whether the page may offer, and switch to, this backend.

    Retired backends stay registered — their protocol code is real and an operator may set
    `CSD_AGENT_BACKEND` to one deliberately — but they are not choices the page presents.
    """
    ensure_registered()
    return backend_name in _OFFERED


def onboard_kinds() -> list[dict]:
    """Every backend that declares how an agent opens its own door (§25).

    Collected from the modules rather than listed here, for the same reason `discover()` scans
    rather than enumerates: a new backend that knows how to be reached adds one attribute and
    appears in the document an agent reads, without this file learning about it.
    """
    found: list[dict] = []
    for name in sorted(ensure_registered()):
        try:
            module = importlib.import_module(f"{_backends_pkg.__name__}.{_module_for(name)}")
        except Exception:
            logger.exception("could not read the onboarding notes for %s", name)
            continue
        declaration = getattr(module, ONBOARD_ATTR, None)
        if isinstance(declaration, dict) and declaration.get("kind"):
            entry = dict(declaration)
            # The label comes from the module's own registration rather than from the
            # onboarding declaration: two copies of a name is one copy too many.
            entry.setdefault("label", getattr(module, LABEL_ATTR, entry["kind"]))
            found.append(entry)
    return found


__all__ = [
    "BACKEND_BLURB_ATTR",
    "OFFERED_ATTR",
    "ONBOARD_ATTR",
    "available",
    "BackendEvent",
    "build_backend",
    "default_backend",
    "describe",
    "discover",
    "ensure_registered",
    "has",
    "is_offered",
    "offered",
    "onboard_kinds",
]
