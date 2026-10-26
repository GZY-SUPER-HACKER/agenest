"""Seed realistic multi-harness data so the sidebar can actually be judged.

Two harnesses with parallel sessions, a spread of statuses, and live progress on the running
ones — the layout only proves itself under more than one harness and more than one project
per harness.

One session is seeded *blocked on the user* (`awaiting_user`), because that state is the one
the overview groups on besides failure, and it is the only one that cannot be produced by
poking at the API with a status patch — it is an event, not a status.

`--fresh` backdates nothing: it just re-seeds with timestamps as of now, so the "maybe
offline" state does not blanket every row 45 seconds after the seed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

BASE = os.environ.get("CSD_BASE_URL", "http://127.0.0.1:8780")
TOKEN = (ROOT / "backend" / "data" / "dev-token").read_text(encoding="utf-8").strip()
H = {"X-CPD-Token": TOKEN}

# Workspaces are only shown in the UI, so any directory works. Point this at wherever your
# projects live if you want the seeded rows to look like your own machine.
WORKSPACE_ROOT = Path(os.environ.get("CSD_SEED_WORKSPACE_ROOT", "~/code")).expanduser()

# (harness, project, title, status, [steps])
SEED = [
    (
        "claude_code",
        "webapp",
        "Fix duplicate compaction in the session loop",
        "running",
        [
            "Read the current autocompact implementation",
            "Write a regression test: one turn compacted twice",
            "Run pytest tests/session -x",
            "12 passed in 8.4s",
            "Update the _has_unarchived_messages call sites",
            "Change the ordering guard in loop.py",
            "Waiting on the type check",
            "mypy reports 2 new errors",
            "Fix the annotations",
            "Re-run the full suite",
            "419 passed in 63.1s",
        ],
    ),
    (
        "claude_code",
        "webapp",
        "Write up a reproducer for issue #6214",
        "completed",
        ["Time how long 50 issues take to reproduce", "Put it in a table", "PR opened: #6230"],
    ),
    (
        "claude_code",
        "compiler",
        "Track down the broken Enter-key submit",
        "running",
        [
            "grep the TUI submit path",
            "The Esc sequence is being truncated during parsing",
            "Add a raw-mode regression test",
            "Test passes; start on the parser",
            "Done, reviewing the diff",
        ],
    ),
    (
        "codex",
        "api-server",
        "Bump api-server to main and try CI",
        "running",
        ["Pull latest main", "Run cargo test", "12 failures, 3 look like real regressions", "Triage them one by one"],
    ),
    (
        "codex",
        "billing",
        "Billing schema migration",
        "failed",
        ["Write the COLUMN_MIGRATIONS patch", "Run npm test --workspace=core", "core: 17 failures"],
    ),
    (
        "codex",
        "billing",
        "Fix Windows path encoding",
        "completed",
        ["read_text was missing encoding", "Add encoding='utf-8' in 9 places", "All tests green"],
    ),
    (
        "pi",
        "billing",
        "Read the provider adapter boundary",
        "interrupted",
        ["Read the four contracts in CONTEXT.md", "Start on schema-migrations.ts"],
    ),
    (
        "pi",
        "docs-site",
        "Draft the M09 release notes",
        "running",
        ["Re-read the subagent section of FAILURE-ATLAS", "Write up the three distinguishing criteria"],
    ),
    (
        "claude_code",
        "billing",
        "Align the session lifecycle contract",
        "awaiting_user",
        [
            "Read section 4.4 of CONTEXT.md",
            "The status enum and the implementation disagree",
            "Need a decision on which one wins",
        ],
    ),
]

#: Seeded as an event, not a status patch: a session blocked on a human has no distinct
#: status — it is still `running`. The dashboard reads it off the newest event.
AWAITING_MESSAGE = "Claude needs your permission to run psql against the staging database"


def post(path: str, payload: dict) -> dict:
    r = httpx.post(BASE + path, json=payload, headers=H, timeout=10)
    r.raise_for_status()
    return r.json()


def main() -> None:
    made = 0
    for agent, project, title, terminal_status, steps in SEED:
        session = post(
            "/api/sessions",
            {
                "agent_type": agent,
                "title": title,
                "project_name": project,
                "workspace": str(WORKSPACE_ROOT / project),
            },
        )
        sid = session["id"]
        made += 1

        for i, step in enumerate(steps):
            post(
                f"/api/sessions/{sid}/events",
                {
                    "event_type": "progress",
                    "message": step,
                    "payload": {"step": i + 1, "of": len(steps)},
                },
            )
            # Spread the events over the last few minutes so relative timestamps differ
            # between rows instead of all reading "now".
            time.sleep(0.4)

            # One agent-authored sentence per running session, so the seeded timeline shows
            # the `checkpoint` event without needing a real Claude Code run (design.md §37).
            if terminal_status == "running" and i == 0:
                post(
                    f"/api/sessions/{sid}/events",
                    {
                        "event_type": "checkpoint",
                        "message": f"Now I'll work on: {step}",
                        "payload": {
                            "source": "prose",
                            "turn": 1,
                            "message_id": f"seed-{sid}-{i}",
                        },
                        "event_key": f"seed-checkpoint-{sid}-{i}",
                    },
                )

        if terminal_status in ("running", "awaiting_user"):
            for _ in range(2):
                post(
                    f"/api/sessions/{sid}/events",
                    {"event_type": "heartbeat", "message": "ping"},
                )

        if terminal_status == "awaiting_user":
            # Last, so it is the newest event: the overview reads the head of the timeline to
            # decide the session is blocked, and anything posted after this would clear it.
            post(
                f"/api/sessions/{sid}/events",
                {"event_type": "awaiting_user", "message": AWAITING_MESSAGE},
            )

        if terminal_status == "failed":
            httpx.patch(
                f"{BASE}/api/sessions/{sid}",
                json={
                    "status": "failed",
                    "summary": "core 17 个测试失败，其中 3 个确认是真回归，已回滚我的改动",
                    "exit_code": 1,
                },
                headers=H,
                timeout=10,
            )
        elif terminal_status == "completed":
            httpx.patch(
                f"{BASE}/api/sessions/{sid}",
                json={"status": "completed", "summary": steps[-1], "exit_code": 0},
                headers=H,
                timeout=10,
            )
        elif terminal_status == "interrupted":
            httpx.patch(
                f"{BASE}/api/sessions/{sid}",
                json={"status": "interrupted", "summary": "被叫停了，回头再看"},
                headers=H,
                timeout=10,
            )

    print(f"seeded {made} sessions / {len({s[0] for s in SEED})} harnesses "
          f"/ {len({s[1] for s in SEED})} projects -> {BASE}")


if __name__ == "__main__":
    main()