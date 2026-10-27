"""Reading the hook payload a coding agent writes to stdin.

**The payload is UTF-8 on the wire, so it is decoded as UTF-8 here** — never through
`sys.stdin`'s codec. On Windows that codec is the ANSI code page (GBK on the machine this was
found on) *with* `surrogateescape`, so a Chinese prompt submitted to `UserPromptSubmit` arrived
as mojibake and, whenever the UTF-8 bytes did not happen to form valid GBK pairs, as lone
surrogates. The damage was not cosmetic:

- `session update --title` was refused by the API's string validation (pydantic rejects lone
  surrogates), and because the 422 body echoes the offending value, encoding *that* raised
  `UnicodeEncodeError` — so the client saw a **500** and the title never changed. Sessions kept
  the placeholder `Claude Code · <project>` for their whole life;
- the same prompt is the `progress` event's message, so that report failed too;
- `MessageDisplay` raised `UnicodeEncodeError` while writing `tracked.json`, losing checkpoints.

Session start and end kept working because those payloads carry a `cwd` and an id, not prose.
docs/design.md §43.

`errors="replace"` rather than `strict`: a hook runs on the agent's critical path and must
never raise (see `hooks.run`), and U+FFFD states that a byte could not be decoded instead of
pretending it did.
"""

from __future__ import annotations

import sys


def read_stdin() -> str:
    """The payload from stdin, decoded as UTF-8.

    A replaced `sys.stdin` without a `buffer` (a test's `io.StringIO`) is already text, so it
    is read as-is. Everything a real hook process sees has a buffer.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return buffer.read().decode("utf-8", errors="replace")


__all__ = ["read_stdin"]
