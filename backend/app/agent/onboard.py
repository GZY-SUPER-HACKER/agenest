"""The document that tells a personal agent how to connect itself (docs/design.md §25).

**Who this is written for.** Not the operator — an agent. It is read by a process that has a
shell, a model, and no patience for narrative: it needs the exact command that opens a door on
its own machine, the exact call that registers the address, the call that verifies, and the call
that switches the page over. So the prose is short, the commands are literal, and every failure
it can hit is named with what to do about it.

**Why it lives here and not in `docs/`.** It is generated from the backends themselves: each
backend module declares how its own door opens (`BACKEND_ONBOARD`), and this module assembles
those declarations plus the endpoints that are the same for everyone. A hand-written doc would
be a second answer to "how does nanobot expose itself", drifting from the adapter that speaks it
the first time either changes — the same reason `BACKEND_BLURB` sits in the module.

**Two renderings, one source.** The Markdown is for the model to read; the JSON is for the case
where the agent would rather not parse prose. They are built from the same declarations, so they
cannot disagree.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings

#: Version of this document's shape. Bumped when a field changes meaning, so an agent that
#: cached one can tell.
DOC_VERSION = 1

_PREAMBLE = """\
# Connecting yourself to agenest

agenest is a dashboard with two pages. One watches coding agents that report to it; the other
is a **channel onto you** — the user types there, you answer, and your replies are rendered as
they stream.

**You are the side that opens the door.** agenest never dials out and cannot reach into your
machine; it is a client of whichever agent its user points it at. So "connecting" is two things,
and only the first is yours:

1. **Expose one interface on loopback** — see the kinds below. Nothing is configured on your
   side beyond running a command you already ship.
2. **Tell agenest where it is**, with one HTTP call (below). That is the whole registration,
   and it needs **no token of the user's** — though a door may have a credential of its own, and
   in that case the kind below says which of your own values to send.

Switching the page over to you is the user's click (Settings → Agent), not another call you
make. That is deliberate: that call decides which agent receives everything the user types, so
it stays behind their credential. Register, report what the probe said, and ask them to switch.

Before you start: everything here is on the same machine as agenest, and the only addresses that
will be accepted are loopback (`127.0.0.1`, `::1`, `localhost`). If the dashboard is not on this
machine, say so and stop — a remote personal agent is not a supported shape.
"""


def endpoints_section(base: str = "") -> str:
    """The calls: one the agent makes, and the switch that belongs to the user."""
    root = base.rstrip("/")
    return f"""\
## The calls

Substitute `$BASE` (this service, e.g. `http://127.0.0.1:8780`).

**Register your address** — the only call you make, and it needs no credential:

```bash
curl -sS -X POST "$BASE/api/agent/backends" \\
  -H "Content-Type: application/json" \\
  -d '{{"kind": "<kind>", "url": "http://127.0.0.1:<port>", "label": "<name you go by>"}}'
```

It answers with the stored row **and the result of the probe it just ran**. `last_error` in that
answer is the truth about your door: empty means agenest reached you.

Registering the same address again is always fine (that is what a restart looks like). Naming a
*different* address for the kind that is currently active is refused with `409` — ask your user
to switch the page away first, then register.

**Then ask your user to switch the page to you** — Settings → Agent → `<kind>`. If they would
rather use curl, this is their call (it needs their access token, which you do not have and do
not need):

```bash
curl -sS -X POST "$BASE/api/agent/backend" \\
  -H "X-CPD-Token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{{"backend": "<kind>"}}'
```

`connected: true` in that answer means the channel is live.

**Removing yourself** is theirs too — Settings → Agent, or
`DELETE $BASE/api/agent/backends/<kind>` with their token. It is refused while you are the
active backend, so they switch away first.
"""


def failures_section() -> str:
    return """\
## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `400` mentioning loopback | You registered an address that is not this machine | Use `127.0.0.1`; a remote agent is not supported |
| `400` about the URL | Not a URL, or not `http(s)://` | Fix the address |
| `409` on register | You named a different address for the kind that is the *active* backend | Ask the user to switch away, then register |
| probe says *nothing answering* | The door is not open yet | Start it (see the kinds below), then register again |
| probe says `401` | Your door is up but its credential differs from what you registered | Re-register with the right `token` |
| The user cannot find the switch | The row is in Settings → Agent under your backend name | Tell them the exact `<kind>` |
| The user says the page is silent | You are active but the model behind you is broken | Check your own model configuration — agenest cannot see it |

**Do not** ask for or handle the user's access token: registration needs none, and the switch is
theirs to make. **Do not** restart your own gateway to make this work, and do not edit agenest's
files: neither is needed for any kind below, and restarting the gateway you are talking to the
user through will end the conversation you are in.
"""


def render_markdown(settings: Settings, kinds: list[dict[str, Any]], base: str = "") -> str:
    """The whole document, for a model to read."""
    parts = [_PREAMBLE, _kinds_section(settings, kinds), endpoints_section(base),
             failures_section()]
    return "\n\n".join(part.rstrip() + "\n" for part in parts if part.strip())


def _kinds_section(settings: Settings, kinds: list[dict[str, Any]]) -> str:
    lines = ["## Kinds — pick the one that is you", ""]
    if not kinds:
        return "\n".join(lines + ["(No backend on this deployment declares how to be opened.)", ""])
    for entry in kinds:
        lines.append(f"### `{entry['kind']}` — {entry.get('label') or entry['kind']}")
        lines.append("")
        lines.append(entry.get("summary", "").strip())
        if entry.get("cost"):
            lines.append("")
            lines.append(f"**What it costs:** {entry['cost']}")
        for step in entry.get("steps") or []:
            lines.append("")
            lines.append(f"**{step.get('title', 'Step')}**")
            lines.append("")
            lines.append("```bash")
            lines.append(step.get("shell", "").strip())
            lines.append("```")
            if step.get("note"):
                lines.append("")
                lines.append(step["note"].strip())
        register = entry.get("register") or {}
        if register:
            lines.append("")
            lines.append(
                "Register it as: `"
                + ", ".join(f"{k}={v!r}" for k, v in register.items())
                + "`"
            )
        lines.append("")
    return "\n".join(lines)


def render_json(
    settings: Settings, kinds: list[dict[str, Any]], base: str = ""
) -> dict[str, Any]:
    """The same document as data, for an agent that would rather not parse prose."""
    root = base.rstrip("/")
    return {
        "version": DOC_VERSION,
        "what": (
            "agenest is a dashboard whose agent page is a channel onto a personal agent. You "
            "are that agent. Expose one interface on loopback, register its address, then "
            "switch the page to yourself."
        ),
        "rules": [
            "Only loopback addresses are accepted (127.0.0.1, ::1, localhost).",
            "Registration needs no credential of the user's; switching the page to you is "
            "their click. A door's own token, when it has one, is yours to send.",
            "Do not restart your own gateway, and do not edit agenest's files.",
            "Registering does not switch anything; the switch is a separate, user-owned call.",
        ],
        "endpoints": {
            "register": f"{root}/api/agent/backends",
            "list": f"{root}/api/agent/backends",
            "remove": f"{root}/api/agent/backends/{{kind}}",
            "switch": f"{root}/api/agent/backend",
            "status": f"{root}/api/agent/status",
            "this_document": f"{root}/api/agent/onboard.json",
        },
        "auth": {
            "header": "X-CPD-Token",
            "register": (
                "none of the user's — registration declares an address and switches nothing; "
                "a kind may ask for the agent's own door token"
            ),
            "switch": "the user's access token; the user performs this call",
        },
        "kinds": [
            {
                "kind": entry["kind"],
                "label": entry.get("label"),
                "summary": entry.get("summary"),
                "cost": entry.get("cost"),
                "health": entry.get("health"),
                "steps": entry.get("steps") or [],
                "register": entry.get("register") or {},
            }
            for entry in kinds
        ],
    }


__all__ = [
    "DOC_VERSION",
    "render_json",
    "render_markdown",
]
