"""The markers a relayed prompt uses on the wire.

Kept apart from `notify.py` so `supervisor.py` can strip the instruction half without
importing the module that imports the supervisor. Both halves are plain literals; there is no
logic here beyond cutting one string at one marker.

Why there are two markers:

- `NOTIFY_MARKER` opens a prompt this service sent on its own initiative, and is what the
  page renders as a notice rather than as something the user typed. The agent sees it too, so
  the first line also says "automated" in words.
- `INSTRUCTION_MARKER` separates what the user may read from what only the agent needs. The
  transcript keeps both — the agent must still be told what to do on a re-read — and the wire
  model cuts at the marker so the scaffolding never becomes a bubble. That is the same
  arrangement as the attachment channel line (`attachments.strip`): addressed to the agent,
  removed for the reader.
"""

from __future__ import annotations

#: The first token of an automated prompt. Mirrored in `frontend/src/pages/AgentPage.tsx`.
NOTIFY_MARKER = "[agenest]"

#: Everything from this line onward is instruction, not conversation.
INSTRUCTION_MARKER = "[agenest · instruction]"


def strip_instructions(text: str) -> str:
    """`text` without the agent-only half appended after `INSTRUCTION_MARKER`.

    The marker is matched as a line, so a transcript that merely *mentions* it in prose keeps
    its words. Nothing after it can be user-visible, and the visible half is trimmed rather
    than left with the blank line that separated the halves.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == INSTRUCTION_MARKER:
            return "\n".join(lines[:index]).rstrip()
    return text


__all__ = ["INSTRUCTION_MARKER", "NOTIFY_MARKER", "strip_instructions"]
