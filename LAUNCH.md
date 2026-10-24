# LAUNCH.md — the release checklist, deliberately not executed yet

Written 2026-10-22. This file exists so the decision to publish is made **once**, against
observable conditions, instead of being re-litigated every week.

Do not execute Phase 1–4 until the gate below is met. Phase 0 is always allowed.

## The gate

Publish when all three are true:

1. **Stable for two weeks.** No data loss, no silent failure, no restart needed to recover.
   The measure is the service you run, not the test suite.
2. **Three real tasks.** Three pieces of actual work done through the Agent tab without
   wanting to bypass it and go back to the terminal.
3. **Interest still present.** If it drops before 1 and 2 are true, publish at 70% instead.
   `me.md` records that interest in a project degrades without external milestones — that
   risk is the reason this file has dates at all.

Review dates: **2026-12-15** (first look, honest yes/no), **2026-12-31** (go, or postpone to
March with a written reason).

Why those dates: 2027 summer-internship applications open around Feb–Apr. Discovery — search,
awesome lists, citations — takes two to three months to accumulate. Publishing later leaves a
line on a CV; publishing then leaves evidence.

## Stop condition

Thirty days after launch: **50 stars, or one external issue/fork** — met. The project then
enters maintenance-only (compatibility fixes, ~2–4 h/month) and the main line goes back to VQA
and the internship. Star growth is not a goal of its own; see "Explicit non-goals".

## Explicit non-goals

- **No hosted SaaS.** The service executes commands and ssh's with the operator's keys;
  multi-tenancy breaks the security model, not the business model.
- **No orchestration, no remote command surface on `/api/sessions`, no inferred state.** The
  narrowness is the asset (design.md §1, §2), not a backlog.
- **No continuous content operation.** That is a different career — indie developer/creator —
  and `me.md` already ranks it below the day job.

## Phase 0 — anytime, ~10 minutes

- [x] Repo description names the agents, so search matches them (2026-10-22).
- [x] Topics cover `claude-code`, `codex`, `pi-coding-agent`, `self-hosted`, `observability`,
      `mobile` (2026-10-22).
- [ ] Profile README: three lines, what you are working on, two or three pinned repos. ~1 hour,
      and it fixes the "notebook cover" problem without touching this repo.

## Phase 1 — the first screen, ~1 day

- [ ] Rewrite the first 15 lines of `README.md`: one sentence of pain, one GIF, one command.
      Keep the screenshot table — below the fold, as detail.
- [ ] Record a 30–45 s GIF: phone, sessions list changing → one session running → completed →
      a reply on the Agent tab. No narration; the cursor does the talking.
- [ ] Same recording is the launch asset — one take, three uses.

## Phase 2 — a read-only demo, 3–5 days

The highest-value item, and the only answer to `has_demo=false`.

- [ ] `CSD_DEMO_MODE=1`: every write route 403, every agent route 403, attach disabled. One
      flag checked in the same places the credentials are — not a separate build.
- [ ] Seed with `seed_dev_data.py` plus a synthetic day of agent conversation.
- [ ] Deploy on the existing cloud host; token in the URL fragment (the mechanism already
      exists); reset nightly.
- [ ] `README`: a "Try it" link above "Quick start".

Residual risk: a demo that can execute is a remote shell. Demo mode must refuse at the route
layer, never by hiding a button.

## Phase 3 — one launch, ~1 day

English first — the audience that files issues is there — Chinese second.

- [ ] Show HN: the title names the question, not the product. The body answers "why not just
      SSH", and says what it deliberately does not do.
- [ ] r/ClaudeAI, r/LocalLLaMA, r/selfhosted — a different first paragraph each; no copy-paste.
- [ ] V2EX 分享创造, 即刻, 少数派.
- [ ] X thread: four tweets, GIF in the first.
- [ ] Timing: Tue/Wed 21:00–23:00 Beijing (US morning).

## Phase 4 — long tail, ~1 hour total

- [ ] `awesome-claude-code`, `awesome-selfhosted`, `awesome-ai-agents`. **After** launch, not
      before: most lists have a star/license/demo bar a pre-launch repo fails, and a rejected
      entry is harder to retry than a first one.
- [ ] Ecosystem lines — Pi, nanobot, OpenClaw docs: "Agenest supports it". This is the only
      contribution to a large repo that returns traffic to this one.

## The six stories this project has to be able to tell

Not documentation — what you say, without notes, when asked "what was hard here".

| # | Claim | Where |
|---|---|---|
| 1 | Status is reported, never inferred; a guessed state is worse than none | design.md §2 |
| 2 | The SSE cursor is a database event id, so it survives a restart; subscribe before replay, because a frame delivered zero times is unrecoverable | §4, §5 |
| 3 | Idempotency is `event_key`, not a timestamp — two reporters can report the same thing in the same millisecond | §7 |
| 4 | Terminal states are sticky and first-write-wins, or the verdict follows packet arrival order | §3 |
| 5 | On Windows `os.kill(pid, 0)` kills the process, and a scheduled task is reaped after 72 h — which is how a tunnel silently stops on day four | §32 |
| 6 | A file crosses the channel as a path in the message text, because no agent protocol has a media frame and every agent has file tools | §29 |

## While polishing (until the gate)

- Fix friction you actually hit daily. Add no features for imagined users.
- Do not touch the reporter contract, the backend seam, or the schema.
- Keep `seed_dev_data.py` and `./dev.sh` working — they are the demo's only prerequisite, and
  the only thing in this list that costs nothing to protect.
- Park new ideas in design.md's open questions, not in the code.
