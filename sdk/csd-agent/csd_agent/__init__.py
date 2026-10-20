"""`csd-agent` — put this machine's coding agents on Agenest.

The dashboard never dials out (docs/design.md §1) and binds loopback, so the machine the
agent runs on has to originate the connection. This package is that side of it: it owns the
tunnel, the credentials, the liveness probe, and the harness adapters, so that "connect this
host" is one command instead of nine with four silent failure modes.

The pieces, and what each is for:

- `install` — vendors `cpctl`, writes the env file and the systemd user units, merges the
  Claude Code hooks, and verifies the result. The only command you run by hand.
- `heartbeat` — a timer-driven probe that reports whether each tracked Claude Code process
  is still alive. `cpctl`'s `heartbeat` event exists for exactly this (design.md §8).
- `hook` — the Claude Code hook entry point. Reads the payload on stdin, calls `cpctl`,
  always exits 0.
- `status` — what this host believes about itself and whether the path is up.

Nothing here is a daemon. It is deliberately three small stdlib-only modules under
`~/.csd`, because the machine this was built against is a shared lab server with no sudo, a
system Python older than the code's declared floor, and other people's conda environments
that must not be touched.
"""

__version__ = "0.1.0"
