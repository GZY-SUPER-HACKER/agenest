"""The liveness probe.

The problem this exists for: a long task — a 40-minute training run, a big refactor, a test
suite — produces no hook events. Claude Code is working, and to the dashboard it looks
identical to a process that died an hour ago. "Maybe offline" is honest but useless.

So this runs on a timer and asks the one question a machine can only ask about itself: *is
the Claude Code process I recorded still there?* That answer is a **fact**, not an
inference, which is what keeps it on the right side of design.md §2.

Two things follow from where the answer comes from:

- It has to run **on the agent host**. Whether a pid is alive is not knowable from the
  cloud, and making it knowable would mean giving the cloud a credential to this machine.
- It reports a **heartbeat**, which by design (design.md §8) renews *liveness* and not
  *activity*. It advances `last_activity_at` so the UI stops calling the session stale, and
  deliberately does not touch `updated_at`, so a session that is merely alive never outranks
  one that actually reported work. It cannot say "in progress" — nothing outside Claude Code
  can know that, and a dashboard that guessed would be worse than one that admits it does
  not know.

The other job here is closing out sessions that ended without saying so: a SIGKILL, a
dropped SSH session, a laptop lid. Nobody will ever fire `SessionEnd` for those, so the
process being gone *is* the report.

And a third, which is why this module does anything on a host with nothing running on it:
reporting the host itself (`report_host`), so a machine that has connected shows up on the
dashboard before its first session — and after its last one.
"""

from __future__ import annotations

import time

from . import access, host, runtime, tracked


def run(verbose: bool = False) -> int:
    # Before anything else, and *before* the early return below. The host exists whether or
    # not anything is running on it, and returning early was exactly why a freshly connected
    # machine stayed invisible until its first Claude Code session started — leaving "did my
    # agent host connect?" with no answer that did not involve SSH.
    report_host(verbose=verbose)

    # The other half of the same errand (design.md §24): the dashboard may have a key waiting
    # for this machine, put there because the user approved it. Installing it is the one part of
    # ssh access only this side can do, and doing it here means an approval takes effect on the
    # next heartbeat with nothing new to schedule.
    access.sync(verbose=verbose)

    entries = tracked.load()
    if not entries:
        if verbose:
            print("no tracked sessions")
        return 0

    live = 0
    closed = 0

    for entry in list(entries.values()):
        # A pid we could not determine is not the same as a process that is gone, and
        # conflating them would be the worst possible bug in this file: the probe would
        # announce that a live session had died, and it would do it within five minutes of
        # every session that started under an unusual process tree. Reporting nothing is
        # correct — the session simply goes stale, which is the honest outcome.
        if entry.pid <= 1:
            if verbose:
                print(f"skip       {entry.csd_session_id}  (pid unknown; cannot judge)")
            continue

        if tracked.process_alive(entry):
            result = runtime.cpctl(
                "event", "--session", entry.csd_session_id,
                "--type", "heartbeat",
                "-m", f"alive (pid {entry.pid})",
            )
            if result.ok:
                live += 1
                # Refreshing this is what keeps a genuinely long session from being pruned:
                # pruning is on contact, not age, so a session that keeps reporting keeps
                # its place. See tracked.prune().
                entry.last_seen_at = time.time()
                tracked.put(entry)
                if verbose:
                    print(f"heartbeat  {entry.csd_session_id}  pid {entry.pid}")
            elif verbose:
                print(f"heartbeat FAILED  {entry.csd_session_id}: {result.detail}")
            continue

        # Gone. Report the closure rather than leaving it at `running` forever.
        result = runtime.cpctl(
            "session", "finish", "--session", entry.csd_session_id,
            "--status", "interrupted",
            "--summary", f"Claude Code process {entry.pid} is gone; no SessionEnd was fired",
        )
        if result.ok:
            tracked.drop(entry.cc_session_id)
            closed += 1
            if verbose:
                print(f"closed     {entry.csd_session_id}  (pid {entry.pid} gone)")
        elif verbose:
            # Deliberately NOT dropped: the server still believes it is running, and
            # forgetting it locally would mean nobody ever tells the server otherwise.
            print(f"close FAILED  {entry.csd_session_id}: {result.detail} (will retry)")

    # Anything left that is both dead and unreportable for hours gets forgotten, so
    # tracked.json cannot grow forever on a host whose dashboard is unreachable.
    tracked.prune()

    if verbose:
        print(f"{live} alive, {closed} closed, {len(tracked.load())} still tracked")
    return 0


def report_host(*, verbose: bool = False) -> None:
    """Say this host is here. Runs even when there is no session to probe.

    Separate from the probe above because the two answer different questions — *is this
    machine reachable* versus *is that process still there* — and the second is meaningless
    when there is nothing to look for. It reports only `connected`: there is no honest way for
    this function to say a host disconnected, because the case that matters (power cut, dead
    network) is precisely the one where it never runs. The dashboard renders that silence as
    "maybe offline".
    """
    result = host.report(connected=True)
    if verbose:
        if result.ok:
            print(f"host       {result.value or 'reported'}")
        else:
            print(f"host report FAILED: {result.detail} (will retry next run)")
