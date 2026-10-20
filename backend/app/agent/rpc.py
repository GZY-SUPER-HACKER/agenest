"""Driving a Pi process over its JSONL RPC protocol.

Pi (`pi --mode rpc`) speaks newline-delimited JSON on stdin/stdout: commands in, a
`response` frame per command, and agent events streamed as they happen. This module owns
exactly that transport and nothing else — no persistence, no policy, no SSE. Those live in
`supervisor.py` and `service.py`, so the protocol can be tested against a stub process.

Two details here are load-bearing and easy to get wrong:

**Framing is LF-only, and that is not a formality.** Pi's RPC documentation is explicit that
clients must split on `\\n` alone, and warns that generic line readers break the protocol
because they also treat `U+2028`/`U+2029` as terminators — both are valid *inside* a JSON
string. Python has the mirror-image hazard: opening the pipe in text mode enables universal
newlines, which splits on `\\r` as well and translates it away, corrupting any message
containing a carriage return (a shell transcript, a diff, a CRLF file). So records are read
as **bytes**, split on `b"\\n"` only, and decoded per record.

**The read limit is raised deliberately.** `asyncio`'s default stream limit is 64 KiB per
line, and `readuntil` raises `LimitOverrunError` past it. A single `tool_execution_end` frame
carries the tool's whole result — a file read, a test log — so the default would fail
precisely on the messages that matter most.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("csd.agent.rpc")

#: Per-line ceiling. See the module docstring: the default 64 KiB is smaller than an ordinary
#: tool result, and the failure mode is a dropped frame in the middle of a conversation.
STREAM_LIMIT = 8 * 1024 * 1024

#: How many stderr lines to keep for diagnostics. Pi's stderr is not part of the protocol,
#: but when a provider key is missing it is the only place that says so.
STDERR_RING = 200


class PiProtocolError(RuntimeError):
    """A frame arrived that was not valid JSON, or was not an object."""


class PiCommandError(RuntimeError):
    """Pi answered a command with `success: false`."""

    def __init__(self, command: str, response: dict[str, Any]) -> None:
        self.command = command
        self.response = response
        super().__init__(f"{command} failed: {response.get('error') or response}")


class PiTimeout(RuntimeError):
    """No response frame arrived within the configured window."""


def encode_command(command: dict[str, Any]) -> bytes:
    """Serialise one command. `ensure_ascii=False` keeps non-Latin prompts readable on the
    wire; the encode step is what actually guarantees a single well-formed record."""
    return (json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def decode_record(record: bytes) -> dict[str, Any] | None:
    """Parse one LF-delimited record.

    Returns `None` for a blank line, which Pi does not send but which costs nothing to
    tolerate. Raises `PiProtocolError` rather than returning a sentinel for malformed JSON:
    a silently skipped frame is a conversation with a hole in it, and the hole is invisible.
    """
    # A `\r\n` line leaves the `\r` intact because we split on `\n` only — that is the
    # protocol-correct behaviour. JSON permits trailing whitespace, but strip it anyway so
    # the record is what the docs describe rather than what happens to parse.
    text = record.rstrip(b"\r").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiProtocolError(f"undecodable record: {text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise PiProtocolError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


#: Extensions Windows will actually start. `.cmd` is here because that is what npm uses.
_RUNNABLE_EXTENSIONS = (".exe", ".cmd", ".bat", ".com")


def resolve_executable(name: str) -> str:
    """Find a runnable command for `name`, preferring one this OS can actually launch.

    Two Windows traps, both hit for real while wiring this up:

    1. `CreateProcess` does not apply `PATHEXT`, so a bare `pi` raises
       "the system cannot find the file specified" even though `pi --version` works in a
       terminal. `shutil.which` does apply it, which is why resolution happens here.
    2. npm installs a CLI as **three** files side by side — `pi` (a POSIX shell script),
       `pi.cmd`, and `pi.ps1`. `shutil.which("pi")` happily returns the extensionless shell
       script, which Windows then refuses with "not a valid Win32 application". So an
       extensionless hit is not accepted as final on Windows; the runnable variant is looked
       up explicitly.

    On POSIX none of this applies and the first hit wins.
    """
    direct = shutil.which(name)
    if os.name != "nt":
        return direct or name

    if direct and os.path.splitext(direct)[1].lower() in _RUNNABLE_EXTENSIONS:
        return direct
    for ext in _RUNNABLE_EXTENSIONS:
        found = shutil.which(name + ext)
        if found:
            return found
    # Nothing runnable found. Return whatever we have so the error names the real problem
    # rather than an invented path.
    return direct or name


class PiProcess:
    """One Pi process, driven over RPC.

    A Pi process *is* a session: `--session-id` decides which JSONL file it reads and
    appends to. So the lifetime of this object and the lifetime of one conversation are the
    same thing, which is why the supervisor keeps one per thread rather than pooling them.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        on_event: EventSink | None = None,
        command_timeout: float = 30.0,
    ) -> None:
        if not argv:
            raise ValueError("argv must not be empty")
        self.argv = list(argv)
        self.cwd = cwd
        self._env = env
        self._on_event = on_event
        self.command_timeout = command_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._counter = 0
        self._stderr: list[str] = []
        #: Set once the process is gone, with the reason. The supervisor reads this to
        #: decide whether a thread needs restarting.
        self.exit_reason: str | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        if self.alive:
            return
        if self.cwd is not None:
            self.cwd.mkdir(parents=True, exist_ok=True)

        # Resolve `argv[0]` before spawning — see `resolve_executable` for the two Windows
        # traps this avoids. On POSIX it is a no-op that also gives the log a real path.
        self.argv = [resolve_executable(self.argv[0]), *self.argv[1:]]

        env = dict(os.environ if self._env is None else self._env)
        # Lets a nested tool or hook notice it is running under the dashboard's agent,
        # in the same spirit as Pi's own AI_AGENT marker.
        env.setdefault("CSD_AGENT", "1")

        logger.info("starting pi: %s (cwd=%s)", " ".join(self.argv), self.cwd or os.getcwd())
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd) if self.cwd else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            # `OSError`, not just `FileNotFoundError`. The failures users actually hit here
            # are: nothing installed (FileNotFoundError), the package behind the shim missing,
            # and a Windows binary that cannot be executed at all (WinError 193). All three
            # are the same problem from the caller's side — the agent will not start — and all
            # three deserve a message naming the fix rather than a traceback.
            raise RuntimeError(
                f"could not start {self.argv[0]!r}: {exc}. "
                "Install it (npm i -g @earendil-works/pi-coding-agent) or point CSD_PI_BIN at one."
            ) from exc

        self.exit_reason = None
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

    async def stop(self, *, grace: float = 5.0) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                # A Pi process stuck mid-tool does not always honour SIGTERM.
                proc.kill()
                await proc.wait()
        self._finish("stopped")

    async def wait(self) -> int | None:
        if self._proc is None:
            return None
        return await self._proc.wait()

    def _finish(self, reason: str) -> None:
        """Mark the process gone and fail anything still waiting on a response.

        Without this, a crashed Pi leaves every in-flight `request()` blocked until its
        timeout — the caller sees a 30-second stall instead of the real error.
        """
        if self.exit_reason is None:
            self.exit_reason = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(f"pi exited: {reason}"))
        self._pending.clear()

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            while True:
                try:
                    # `readuntil`, not `readline`: it splits on exactly this separator and
                    # nothing else, which is the whole contract (see module docstring).
                    record = await stream.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        await self._dispatch(exc.partial)
                    break
                except asyncio.LimitOverrunError as exc:
                    # Should be unreachable at STREAM_LIMIT, but a silently dropped frame is
                    # the worst outcome here, so it is logged loudly rather than swallowed.
                    logger.error("pi frame exceeded the %d-byte limit: %s", STREAM_LIMIT, exc)
                    break
                await self._dispatch(record)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("pi stdout reader failed")
        finally:
            code = await self._proc.wait()
            self._finish(f"exited with code {code}")

    async def _dispatch(self, record: bytes) -> None:
        try:
            frame = decode_record(record)
        except PiProtocolError as exc:
            # One bad frame is not a reason to tear down a working conversation, but it is
            # a reason to say so.
            logger.warning("skipping malformed pi frame: %s", exc)
            return
        if frame is None:
            return

        if frame.get("type") == "response":
            request_id = frame.get("id")
            future = self._pending.pop(request_id, None) if request_id else None
            if future is not None and not future.done():
                future.set_result(frame)
            else:
                # An unmatched response means the caller already timed out. Logged, not
                # raised: the conversation is still usable.
                logger.debug("unmatched pi response: %s", frame)
            return

        if self._on_event is None:
            return
        result = self._on_event(frame)
        if asyncio.iscoroutine(result):
            await result

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stream = self._proc.stderr
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr.append(text)
                del self._stderr[: max(0, len(self._stderr) - STDERR_RING)]
                logger.debug("pi stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("pi stderr reader failed")

    def stderr_tail(self, limit: int = 20) -> str:
        """The last few stderr lines, for the error a user actually sees.

        A missing API key surfaces only here, so a failed start that reported an exit code
        and nothing else would be actively unhelpful.
        """
        return "\n".join(self._stderr[-limit:])

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #
    async def request(
        self, command: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one command and await its `response` frame.

        Pi answers a `prompt` as soon as it is accepted, not when the run finishes, so a
        response means "received", not "done". Progress arrives as events.
        """
        if self._proc is None or self._proc.returncode is not None:
            raise RuntimeError(f"pi is not running ({self.exit_reason or 'not started'})")
        assert self._proc.stdin is not None

        self._counter += 1
        request_id = f"req-{self._counter}"
        payload = {**command, "id": request_id}

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        self._proc.stdin.write(encode_command(payload))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"pi closed its input: {exc}") from exc

        try:
            response = await asyncio.wait_for(future, timeout=timeout or self.command_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise PiTimeout(
                f"no response to {command.get('type')!r} within {timeout or self.command_timeout}s"
            ) from None

        if response.get("success") is False:
            raise PiCommandError(str(command.get("type")), response)
        return response


__all__ = [
    "PiCommandError",
    "PiProcess",
    "PiProtocolError",
    "PiTimeout",
    "decode_record",
    "encode_command",
    "resolve_executable",
]
