# Security policy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security → Report a vulnerability)
rather than a public issue. If that is unavailable, contact the repository owner directly and
include a minimal reproduction, the affected route or file, and the impact you believe it has.

This is a self-hosted project maintained on a best-effort basis. There is no bug bounty.

## What the design already assumes

The threat model is written down in [docs/design.md](docs/design.md), and the short version
matters for triage:

- The dashboard is **read-only** for everything under `/api/sessions`; it has no code path that
  executes anything on the machine the work runs on.
- There are two credentials. `CSD_INGEST_TOKEN` authenticates **writes** (a host reporting a
  session) and is shared by all reporters. `CSD_DASHBOARD_TOKEN` gates the **page** and every
  route that can act, including `/api/agent/*`. With no dashboard token, reads are open and
  everything that *acts* answers 403.
- A **missing** dashboard token is a deliberate state, not a misconfiguration: it makes the
  agent page unusable rather than handing out a shell. Do not report that as a bypass.
- `POST /api/agent/onboard` and `POST /api/agent/backends` are intentionally unauthenticated.
  They only *declare* where an agent listens; they cannot switch the active agent (that call
  stays behind the dashboard token) and they cannot move the address the page is currently
  using. If you find a way to use them to redirect the user's next message, that is a real
  finding — report it.
- Secrets are never returned by the API. The agent's provider key is reported only as
  `api_key_set` / `api_key_env`; SSH private keys stay under the service user's `$HOME/.ssh`.

## Deployment expectations

- Bind loopback (`CSD_HOST=127.0.0.1`) or a private network. Exposing the agent page to the
  public internet is explicitly out of scope; publish only reads through a tunnel or Tailscale
  Funnel, and always with `CSD_DASHBOARD_TOKEN` set.
- Run the service as an unprivileged user with the systemd hardening in
  `deploy/systemd/csd.service`. `MemoryDenyWriteExecute` is off on purpose (V8 JIT); that is a
  documented trade-off, not an oversight.
