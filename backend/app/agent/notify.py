"""Relaying a blocked session to the personal agent.

A coding agent that needs a human fires `awaiting_user` (§16b). That event puts the session
under *Needs attention* — but only for someone who is looking at the board. The personal agent
is the thing the user actually talks to, so the useful behaviour is for **it** to say something
when one of its coding sessions stops to ask:

    session reports `awaiting_user` → this service prompts the personal agent → the agent
    tells the user, in the conversation they already have open

The direction is the same as every other agent call: this service is a *client* of the agent
and dials it (§17). Nothing new executes here, and nothing is inferred — the trigger is a
report, not a hunch that a quiet session looks stuck.

Three decisions are worth stating because the obvious alternative is worse:

**A real agent turn, not a synthetic bubble.** The transcript belongs to the agent (§18); this
service cannot write an assistant message into Pi's session file or nanobot's own record, and
minting one row in a table of our own would be a second, divergent answer to "what was said".
So the notification is a prompt. The agent phrases it, and if the user answers in that
conversation the agent can act on the answer — which is the whole point of it being an agent
rather than a push notification.

**Marked as automated, and told not to act.** The prompt begins with `[agenest]`, and its
instructions are explicit: relay this, do not use tools, do not answer the decision yourself.
An automated turn that starts driving machines on its own initiative is the failure this
feature could introduce, and it is cheaper to state the boundary than to trust a model to
infer it. The page renders the marker as a tag rather than as something the user typed.

**A *new* block, not every repeated notification.** Claude Code re-fires `Notification` while
it stays blocked, and the same event type is used by Codex and DSH. The relay fires only when
the newest display event before this one is not already `awaiting_user`: the session just
became blocked, so there is something the user has not been told. That is stateless (the event
timeline is the state), survives a restart, and costs nothing when a reporter repeats itself.

Best-effort by construction. A notification that fails — no agent configured, the process is
gone, the model errors — must never turn into a failed event report. The reporter's 202 is
about the report, not about whether we could reach the agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import Settings
from ..schemas import EventOut, EventType, SessionOut, SessionStatus, cap_string
from . import store
from .notify_text import INSTRUCTION_MARKER, NOTIFY_MARKER
from .supervisor import Supervisor

logger = logging.getLogger(__name__)


def compose_ask_prompt(session: SessionOut, event: EventOut) -> str:
    """The prompt that asks the personal agent to relay one blocked session.

    Deliberately structured facts followed by rules, rather than a paragraph of prose: the
    agent has to extract the project, the session and the question, and the rules have to
    survive a model that is inclined to be helpful by doing something.
    """
    project = session.project_name or session.workspace or session.title
    where = f" on {session.host}" if session.host else ""
    question = _question(event)
    options = _options(event.payload)

    lines = [
        f"{NOTIFY_MARKER} A coding session is blocked and needs your decision.",
        "",
        f"- Project: {project}",
        f"- Session: {session.title} ({session.id})",
        f"- Agent: {session.agent_type}{where}",
        f"- Waiting since: {event.received_at}",
        f"- What it asks: {question}",
    ]
    if options:
        lines.append(f"- Options reported: {' / '.join(options)}")
    lines.extend(
        [
            "",
            INSTRUCTION_MARKER,
            "Tell the user about this in their language, in one to three short sentences. Use "
            "the shape: <project> session \"<title>\" needs your decision: <what it asks>. If "
            "options are listed above, list them. Name the machine the session is on so they "
            "can find it.",
            "",
            "This is a notification to relay, not a task: do not use tools, do not answer the "
            "decision yourself, do not change any files, and do not drive the session. The "
            "facts above are from agenest, not from your user. If the user replies here with "
            "a choice, then help them act on it.",
        ]
    )
    return "\n".join(lines)


def _question(event: EventOut) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    question = payload.get("question")
    if isinstance(question, str) and question.strip():
        return cap_string(question.strip())
    return event.message


def _options(payload: dict[str, Any] | None) -> list[str]:
    """The choices a reporter gave us, if any.

    No reporter supplies these yet — Claude Code's `Notification` carries a sentence, and
    Codex/DSH name a tool — so this is a convention for the payload rather than a parsed
    field: `payload.options` or `payload.choices`, a list of strings or of objects that wrap
    one in `label` / `text` / `title` / `value` / `name`. A string is one option.

    Tolerant on purpose. A reporter that starts sending choices should not have to match a
    schema this service invented after the fact, and a shape we cannot read costs the option
    list, not the notification.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("options")
    if raw is None:
        raw = payload.get("choices")
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    out: list[str] = []
    for item in items[:20]:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = next(
                (
                    item[key]
                    for key in ("label", "text", "title", "value", "name")
                    if isinstance(item.get(key), str)
                ),
                "",
            )
        else:
            text = ""
        text = text.strip()
        if text:
            out.append(cap_string(text, 200))
    return out


def _is_new_block(db, session_id: str, event_id: int) -> bool:
    """Whether this `awaiting_user` is the start of a block the user has not been told about.

    Compares against the newest **display** event before this one, which is the same ordering
    the board uses (`attach_last_events` takes `MAX(id)` over display events). A repeated
    `awaiting_user` — Claude Code re-fires `Notification` while a prompt stays open — is not a
    new question, and answering it with another agent turn would spend a model call to repeat
    a sentence the user already has.
    """
    row = db.query_one(
        """
        SELECT event_type FROM session_events
         WHERE session_id = ?
           AND id < ?
           AND event_type NOT IN ('heartbeat', 'status_changed')
         ORDER BY id DESC
         LIMIT 1
        """,
        (session_id, event_id),
    )
    return row is None or row["event_type"] != EventType.awaiting_user.value


class AskNotifier:
    """Schedules the relay without making the reporter wait for it.

    One instance per app (it owns the pending tasks), stored on `app.state` and closed on
    shutdown. The event report must stay a 202 that returns in milliseconds: an agent turn can
    take minutes, and a reporter that blocks on it would retry a report the server already
    recorded — the exact failure the ingest contract exists to avoid.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(
        self,
        *,
        db,
        supervisor: Supervisor,
        session: SessionOut,
        event: EventOut,
    ) -> asyncio.Task[None] | None:
        """Start a relay if this report is one, and return the task (tests await it).

        The guards are all cheap and all answer "is there anything to do": the feature used
        to be enabled, the agent appears at all (the agent routes are disabled without an
        access token, so a deployment that cannot open the conversation must not start one),
        the report is `awaiting_user`, and the session is still running — a late
        `awaiting_user` on a closed row is accepted by the API but is not something the user
        can answer, which is the same gate the board applies (§42).
        """
        if not self._settings.agent_notify_on_ask:
            return None
        if not self._settings.dashboard_token:
            # The page that could show or answer this is disabled (`auth.access_header`).
            # Notifying would create a conversation nobody can open.
            return None
        if event.event_type is not EventType.awaiting_user:
            return None
        if session.status is not SessionStatus.running:
            return None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - every caller is an async route
            logger.warning("ask notification dropped: no running event loop")
            return None

        task = loop.create_task(
            self._relay(db, supervisor, session, event),
            name=f"ask-notify:{session.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _relay(
        self, db, supervisor: Supervisor, session: SessionOut, event: EventOut
    ) -> None:
        try:
            if not _is_new_block(db, session.id, event.id):
                return
            backend = supervisor.backend
            if not backend.is_available():
                logger.info(
                    "awaiting_user on %s not relayed: the %s agent is not available",
                    session.id,
                    backend.name,
                )
                return
            # Serialised, so two sessions that block at the same moment cannot create the
            # second conversation this product does not have (§18). The title is only used on
            # first use: a later prompt would otherwise name the thread after a notification.
            thread = store.get_or_create_session(
                db, backend=supervisor.backend_name, title="Session notifications"
            )
            await supervisor.prompt(thread.id, compose_ask_prompt(session, event))
            logger.info(
                "relayed awaiting_user on %s to the %s agent (%s)",
                session.id,
                supervisor.backend_name,
                thread.id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort, and deliberately broad: this runs after the report has been
            # accepted, on a background task nobody awaits. A failure here is a notification
            # the user did not get, never a report the server did not record.
            logger.warning(
                "could not relay awaiting_user on %s to the personal agent",
                session.id,
                exc_info=True,
            )

    async def aclose(self) -> None:
        """Cancel pending relays on shutdown, and wait until they have let go."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["AskNotifier", "compose_ask_prompt"]
