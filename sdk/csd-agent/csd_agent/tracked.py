"""What this host believes is currently running.

`cpctl` caches its session id per working directory (`$CSD_STATE_DIR/<slug>.id`), which is
the right thing for a launcher script and the wrong thing for us: two Claude Code sessions
in one directory would share one cached id, and one of them would report into the other's
timeline. So `csd-agent` keeps its own map, keyed by **Claude Code's** session id, and
always passes `--session` explicitly.

What the map buys, beyond bookkeeping:

- **Liveness.** A CC process that is killed with SIGKILL never fires `SessionEnd`, so its
  session would sit at `running` forever and only ever show as "maybe offline". The stored
  pid lets the heartbeat turn that into a reported fact ("the process is gone") instead of
  an inference from silence.
- **Resume.** `--resume`/`--continue` fires `SessionStart` again for a conversation that
  already has a dashboard session. The map is what lets the hook recognise that and reuse
  the existing session rather than inventing a second one.

The map is keyed by CC's session id but the pid is the load-bearing field, and pids are
reused by the kernel. `started_at` is stored alongside so a heartbeat can sanity-check that
it is looking at the same process and not a recycled number.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

#: How long a tracked entry may go without a successful heartbeat before we stop trusting
#: it. Comfortably longer than the timer interval, so one missed run is not a verdict.
STALE_TRACKED_SECONDS = 3 * 60 * 60


@dataclass
class Tracked:
    cc_session_id: str
    csd_session_id: str
    pid: int
    cwd: str
    started_at: float
    #: When we last successfully talked to the server about this session. Distinct from
    #: `started_at` on purpose: pruning on age would end the tracking of any session older
    #: than the window — which is exactly the long-running session the heartbeat exists to
    #: keep alive. Pruning on *contact* drops only what has become unreachable.
    last_seen_at: float = 0.0
    #: Replies finished in this conversation as far as this connector has seen. It exists to
    #: make a turn's reports unique: `cpctl` derives its idempotency key from the message, and
    #: two turns that carry the same prompt text ("continue") would otherwise collapse into a
    #: single timeline row. A resumed conversation keeps counting rather than restarting.
    turn: int = 0
    #: The assistant message being streamed by `MessageDisplay`, kept here between hook
    #: processes. A message only becomes a checkpoint once a tool batch proves it was
    #: commentary rather than the answer, and that proof arrives in a *later* hook
    #: (`PostToolBatch`). The text is capped so a runaway message cannot grow this file; the
    #: dashboard caps it again on ingest.
    pending_text: str = ""
    #: The agent's own id for that message. It is the checkpoint's idempotency key, so a retry
    #: or a second flush re-sends the same sentence without stacking a row.
    pending_message_id: str = ""
    #: True once `MessageDisplay` reported the message's final batch. A partial message is not
    #: a checkpoint: the model may still be mid-sentence when a tool batch starts.
    pending_final: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _load_raw() -> dict[str, dict]:
    path = config.tracked_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict[str, Tracked]:
    out: dict[str, Tracked] = {}
    for key, raw in _load_raw().items():
        if not isinstance(raw, dict):
            continue
        try:
            started = float(raw.get("started_at") or 0.0)
            out[key] = Tracked(
                cc_session_id=raw.get("cc_session_id", key),
                csd_session_id=raw["csd_session_id"],
                pid=int(raw.get("pid") or 0),
                cwd=raw.get("cwd", ""),
                started_at=started,
                last_seen_at=float(raw.get("last_seen_at") or started),
                turn=int(raw.get("turn") or 0),
                pending_text=str(raw.get("pending_text") or ""),
                pending_message_id=str(raw.get("pending_message_id") or ""),
                pending_final=bool(raw.get("pending_final")),
            )
        except (KeyError, TypeError, ValueError):
            # A malformed row is dropped rather than crashing a hook. Losing one tracked
            # session costs a liveness signal; crashing a hook costs a user's session.
            continue
    return out


def _save(entries: dict[str, Tracked]) -> None:
    path = config.tracked_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: entry.to_dict() for key, entry in entries.items()}
    # A **unique** temporary name, because hooks do run concurrently: closing one VS Code
    # window fires a `SessionEnd` per open session in the same second, and a fixed
    # `tracked.json.tmp` had one process rename the file the other was about to write —
    # `FileNotFoundError` from `os.replace`, and that hook's update lost.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # Windows refuses to replace a file another process has open at that instant, and the
        # other processes here are more hooks writing this same file. A unique name removed
        # the "someone renamed my temp file" failure; the retry covers the remaining one.
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def put(entry: Tracked) -> None:
    entries = load()
    entries[entry.cc_session_id] = entry
    _save(entries)


def drop(cc_session_id: str) -> None:
    entries = load()
    if entries.pop(cc_session_id, None) is not None:
        _save(entries)


def get(cc_session_id: str) -> Tracked | None:
    return load().get(cc_session_id)


def for_cwd(cwd: str) -> list[Tracked]:
    return [e for e in load().values() if e.cwd == cwd]


def prune() -> dict[str, Tracked]:
    """Forget entries we have been unable to say anything about for hours.

    Pruning is on **last successful contact**, not on age. Age would be simpler and wrong:
    it would stop tracking every session older than the window, and the sessions that run
    longest are precisely the ones whose liveness the heartbeat exists to report. Contact
    is the property that actually degrades when the dashboard becomes unreachable, which is
    the situation this guards against.

    An entry with an unknown pid (`0`) is kept on the same terms as any other — we never
    learned enough to judge it, and dropping it early would discard the only record that
    this session exists and is unaccounted for.

    Called by the heartbeat *after* it has had a chance to close out dead sessions, never by
    a hook: a hook pruning state is a hook making a decision about someone else's session,
    and it has neither the time nor the mandate.
    """
    entries = load()
    now = time.time()
    kept = {
        key: entry
        for key, entry in entries.items()
        if (now - max(entry.last_seen_at, entry.started_at)) < STALE_TRACKED_SECONDS
    }
    if len(kept) != len(entries):
        _save(kept)
    return kept


# --------------------------------------------------------------------------- #
# process identity
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _ppid_of(pid: int) -> int:
    """Parent of `pid`, via /proc. Returns 0 when it cannot be determined.

    `/proc/<pid>/stat`'s second field is the command name in parentheses and may itself
    contain spaces and parentheses, so the fields after it are found from the *last* `)`
    rather than by splitting on whitespace — the naive split misreads `(sd-pam)` and
    anything else with a space in its name.
    """
    stat = _read(f"/proc/{pid}/stat")
    if not stat:
        return 0
    close = stat.rfind(")")
    if close == -1:
        return 0
    fields = stat[close + 2:].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0


def process_alive(entry: Tracked) -> bool:
    """Is the Claude Code process we recorded for this session still there?

    `kill(pid, 0)` alone is not enough: it succeeds for a zombie, and it says nothing about
    whether the pid now belongs to a different program. So the start time is checked too —
    `/proc/<pid>/stat` field 22 is the process start time in clock ticks, which is stable
    for the life of a process and changes when a pid is recycled.
    """
    if entry.pid <= 1:
        return False
    if os.name == "nt":
        # **Never `os.kill(pid, 0)` here.** On Windows it is not a probe: the implementation
        # opens the process and calls TerminateProcess with the signal as the exit code. A
        # liveness check that kills the session it is asking about is the worst bug this
        # package could ship. `_win_process_alive` uses OpenProcess instead.
        return _win_process_alive(entry.pid)
    try:
        os.kill(entry.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process on this pid. Not ours.
        return False

    stat = _read(f"/proc/{entry.pid}/stat")
    if not stat:
        return False
    close = stat.rfind(")")
    fields = stat[close + 2:].split() if close != -1 else []
    if len(fields) > 19:
        state = fields[0]
        if state == "Z":
            return False
    return True


def find_cc_pid() -> int:
    """Walk our own parent chain to the Claude Code process, or 0 if not found."""
    return find_pid(("claude",))


def find_pid(prefixes: tuple[str, ...]) -> int:
    """Walk our own parent chain to an ancestor whose name starts with one of `prefixes`.

    A hook is spawned by the agent, so the answer is an ancestor — which is why the wrapper
    `exec`s the interpreter rather than backgrounding it: detaching would reparent us to init
    and the chain would be gone.

    Matching is on the basename of both `exe` and argv[0], because the agents ship as a native
    binary on some platforms and a Node script on others, and the two look nothing alike from
    the outside. Anything under our own home is skipped, so the wrapper that invoked us can
    never be mistaken for the agent.

    `prefixes` is a tuple because two agents share this package: `("claude",)` for Claude Code
    and `("codex",)` for Codex. An unknown pid (0) is a real answer; guessing one would let the
    heartbeat announce the death of a live session.
    """
    if os.name == "nt":
        return _win_find_pid(prefixes)
    skip = str(config.agent_home())
    pid = os.getpid()
    for _ in range(24):
        pid = _ppid_of(pid)
        if pid <= 1:
            return 0

        argv0 = _read(f"/proc/{pid}/cmdline").split("\0", 1)[0]
        try:
            exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            exe = ""

        for candidate in (os.path.basename(argv0), exe):
            if not candidate or skip in candidate:
                continue
            if _name_matches(candidate, prefixes):
                return pid
    return 0


def _name_matches(name: str, prefixes: tuple[str, ...]) -> bool:
    base = name[:-4] if name.lower().endswith(".exe") else name
    return any(base == prefix or base.startswith(f"{prefix}-") for prefix in prefixes)


# --------------------------------------------------------------------------- #
# Windows: the same two questions, asked through kernel32
# --------------------------------------------------------------------------- #

def _win_process_alive(pid: int) -> bool:
    """Does this pid still name a running process?

    **`os.kill(pid, 0)` cannot be used here.** On Windows it is not a probe: Python's
    implementation opens the process and calls `TerminateProcess` with the signal as the
    exit code. A liveness check that kills the session it is asking about is the worst bug
    this package could ship. `OpenProcess` + `GetExitCodeProcess` answer the question
    without touching the process.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _win_find_pid(prefixes: tuple[str, ...]) -> int:
    """Walk our parent chain via Toolhelp32 and return the first matching agent process.

    Toolhelp gives an executable name but not a command line, so an agent launched as
    `node.exe …` cannot be told apart from any other Node process. Returning 0 in that case
    is deliberate: `heartbeat` already treats an unknown pid as "cannot judge", while a
    guessed pid would let the probe announce the death of a live session.
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE:
        return 0

    parent_of: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return 0
        while True:
            parent_of[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                str(entry.szExeFile).lower(),
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)

    pid = os.getpid()
    for _ in range(24):
        parent = parent_of.get(pid, (0, ""))[0]
        if parent <= 1:
            break
        pid = parent
        name = parent_of.get(pid, (0, ""))[1]
        if _name_matches(name, prefixes):
            return pid

    # The chain can break on Windows: a launcher that exits leaves its child pointing at a
    # dead parent pid (there is no reparenting), and the walk then stops before reaching the
    # agent. Falling back to "the only matching executable on this machine" is safe — and if
    # there is more than one, returning 0 is the right answer, because attributing a session
    # to the wrong window would let the heartbeat close a live session.
    candidates = sorted(
        candidate
        for candidate, (_parent, name) in parent_of.items()
        if _name_matches(name, prefixes)
    )
    return candidates[0] if len(candidates) == 1 else 0
