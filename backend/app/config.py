"""Runtime configuration, read from environment variables.

Design notes:
- The ingest token is **required**. There is no insecure default: a missing token is a
  startup error rather than a silently open server.
- The dashboard bind address defaults to loopback. Binding a Tailscale address or
  0.0.0.0 is an explicit, operator-made choice (see deploy/README.md).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the environment is not sufficient to run safely."""


#: Provider keys forwarded to the Pi process, by environment variable name.
#:
#: Pi resolves its own credentials from the environment, so a key that is present in the
#: service's environment is all it needs. Listing them here rather than passing everything
#: through is deliberate: the set is then documented, reviewable, and small enough to reason
#: about — and `CSD_PI_ENV_KEYS` can extend it for a provider not named here.
DEFAULT_PI_ENV_KEYS = ",".join(
    (
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "AZURE_OPENAI_API_KEY",
    )
)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class Settings:
    db_path: Path
    #: Required. What a *host* presents when it reports work — `POST /api/sessions`, events,
    #: `PATCH`. Deliberately not the page credential: it has to be issuable to machines that
    #: should not be able to drive an agent.
    ingest_token: str
    #: The one credential for this service's own page: reads, closing a session, and driving the
    #: agent. Unset means reads are open and the routes that **act** are disabled — see
    #: `auth.access_header`. There used to be three page tokens (`control_token`, `agent_token`);
    #: they were collapsed on the operator's call (docs/design.md §22).
    dashboard_token: str | None
    host: str
    port: int
    static_dir: Path | None
    stale_after_seconds: int
    event_payload_max_chars: int
    cors_origins: tuple[str, ...]
    # --- agenest (the personal agent) ------------------------------------- #
    #: Which agent a fresh deployment talks to. `native` by default so the page works with
    #: nothing but a provider key; switchable at runtime from the settings panel.
    agent_backend: str = "native"
    #: The nanobot gateway's websocket channel. nanobot's every channel is one of these; this
    #: service is simply another client of it. A door the agent registered (`agent/endpoints.py`)
    #: wins over this address, so a self-connected agent needs neither setting.
    nanobot_ws_url: str = "ws://127.0.0.1:8765/"
    #: Identifies this dashboard to nanobot, for its `allowFrom` allow-list. Stable by
    #: default: a random id per launch could never be allow-listed in advance.
    nanobot_client_id: str = "csd-dashboard"
    #: nanobot's credential for that door, when one is configured: a static
    #: `channels.websocket.token`, or the `tokenIssueSecret` that `/webui/bootstrap` checks
    #: before minting a one-time socket token. This is *nanobot's* credential and is scoped to
    #: nanobot — it grants nothing on this host, unlike `dashboard_token`.
    nanobot_token: str | None = None
    #: OpenClaw's Gateway WebSocket. Unlike nanobot there is no channel token to mint:
    #: access is a **device pairing** approved on the gateway host (`openclaw devices
    #: approve <requestId>`), so the settings panel asks a person to run that rather than
    #: guessing at a credential. `caps` advertises the capabilities this client implements —
    #: and omitting `tool-events` silently disables live tool streaming, so it is pinned on.
    openclaw_ws_url: str = "ws://127.0.0.1:18789/"
    openclaw_token: str | None = None
    openclaw_caps: tuple[str, ...] = ("tool-events",)
    #: Hermes' **API server**, which is the surface this service uses — `/v1/...` and
    #: `/api/sessions/...` over HTTP, with SSE for a streaming reply.
    #:
    #: The rejected option is worth recording, because it is the one the earlier research
    #: landed on: Hermes' *Desktop app* also runs a canonical WebSocket on this same machine,
    #: but its requests are gated by short-lived `session-ticket` grants minted over a
    #: same-OS-user control socket, and its protocol is documented as the desktop
    #: application's own inner wiring. That is another product's private surface — the same
    #: dependency that kept this service off nanobot's WebUI route — and it would break the
    #: day Hermes reshuffles its front end. The API server is documented as an integration
    #: surface for other front ends, which is what this is.
    hermes_api_url: str = "http://127.0.0.1:8642"
    #: Hermes' `API_SERVER_KEY`, sent as a bearer token. **Required to be useful**: without
    #: it every request is a 401, so the settings row reports the credential as missing
    #: rather than inviting someone to check whether the server is running.
    hermes_api_key: str | None = None
    #: The model name the OpenAI-compatible surface expects. `hermes-agent` selects Hermes'
    #: own configured provider instead of naming a vendor's model, which is what we want: the
    #: model is the agent's business, not the channel's.
    hermes_model: str = "hermes-agent"
    #: The command that starts Pi in RPC mode, as an **argv list**. A list rather than a
    #: string so a test can substitute its own stub process, and so a path with spaces in
    #: it does not silently become two arguments.
    pi_bin: tuple[str, ...] = ("pi",)
    #: Extra argv appended after `--mode rpc`. Kept separate from `pi_bin` so the base
    #: command stays recognisable in logs.
    pi_args: tuple[str, ...] = ()
    #: The directory the agent works in. Deliberately **required to be explicit**: defaulting
    #: this to the repository root would hand a bash-capable agent write access to the
    #: dashboard's own source the first time someone enabled the feature.
    pi_workspace: Path | None = None
    #: Where Pi keeps its JSONL session files. One file per thread, so a restart resumes
    #: the same conversation instead of starting a blank one.
    pi_session_dir: Path | None = None
    #: Pi's built-in default provider is `google`. Passing neither of these therefore lands
    #: on a provider most people have no credential for, which reads as "the agent is broken"
    #: rather than "no model was configured". Both are surfaced in `pi --help` terms.
    pi_provider: str | None = None
    pi_model: str | None = None
    #: Provider API keys handed to the Pi process, by environment variable name.
    #:
    #: Pi reads its own keys from the environment (`DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, …),
    #: so the only question is whether *this service's* environment has them. Under systemd
    #: it does not unless the EnvironmentFile carries them, and a key that exists in your
    #: shell but not in the unit is a confusing "no API key found" from a process you cannot
    #: see. So the well-known ones are collected explicitly and passed through, which also
    #: makes it possible to state in the docs exactly what this service forwards.
    pi_env_keys: tuple[str, ...] = ()
    #: Tool allowlist passed through to Pi (`--tools`). Empty means "Pi's own default", which
    #: is every built-in tool **including `bash`** — i.e. arbitrary command execution on this
    #: host. See docs/design.md §17.
    pi_tools: tuple[str, ...] = ()
    #: Seconds to wait for a `response` frame before treating a command as failed. Pi answers
    #: a `prompt` as soon as it is accepted, so this bounds acceptance, not the whole run.
    pi_command_timeout: float = 30.0
    #: Minutes a conversation must be idle before the service asks Pi to compact its context.
    #: **Zero disables it.**
    #:
    #: Pi already compacts on its own when the context nears the window; this is the proactive
    #: half. It exists because this agent has exactly one conversation that never ends (§19), so
    #: the context only grows — and because an idle agent is the cheapest moment to spend a
    #: summarization call, when nobody is waiting on it.
    agent_idle_compact_minutes: float = 15.0
    #: How often the idle scan runs. Sixty seconds matches nanobot's own scan interval.
    agent_idle_compact_check_seconds: float = 60.0
    #: --- memory (docs/design.md §18) ---------------------------------------- #
    #: Directory the agent's long-term memory files live in. Defaults to a `memory/` inside the
    #: agent workspace, so the memory follows the agent rather than the database: `MEMORY.md` and
    #: `USER.md` are readable with `ls`, which is the entire point of the filesystem route.
    #:
    #: Deliberately **not** inside the repository, and deliberately not the workspace root: the
    #: workspace is where the agent works, and a `MEMORY.md` sitting beside the code under
    #: development is one `rm -rf` away from gone.
    memory_dir: Path | None = None
    #: Hours between automatic Dream runs. **Zero disables the schedule.**
    #:
    #: On by default, at nanobot's own interval (`DreamConfig.interval_h` = 2). The first version
    #: of this defaulted to off, on the argument that a channel must not spend the operator's
    #: model budget on its own initiative. That argument was answered rather than overruled: once
    #: the agent re-reads its memory on every prompt, an empty memory is a visible hole in every
    #: answer rather than a cost avoided, and a layer nobody turns on is a layer that does not
    #: exist. The knob for the cost is this number — raise it, or set 0 and run Dream by hand.
    memory_interval_h: float = 2.0
    #: Journal entries per Dream batch. Bounds one run's reading; the cursor means a long journal
    #: is consumed over several runs rather than all at once.
    memory_dream_batch: int = 20
    #: Skip journaling a conversation whose last run produced fewer than this many characters of
    #: new transcript. Below it, the run was a greeting or a typo, and summarising it would bury
    #: the useful entries under noise.
    memory_journal_min_chars: int = 400
    #: Override the model Dream runs on. Empty means "the agent's own model", which is right in
    #: practice: Dream reads the same memory it curates, and a different model would read a
    #: different dialect of the same files.
    memory_model: str | None = None
    #: --- progress notes (docs/design.md §23) --------------------------------- #
    #: Whether the agent is asked to write a **structural checkpoint** — one short line per stage,
    #: in the message that carries that stage's tool calls — rather than a per-tool-call feed.
    #:
    #: On by default, and that is a reversal of nanobot's own default on purpose. There the switch
    #: promised output a default install could not produce, so it defaulted to false; here the
    #: instruction that produces it ships with the service, and off would mean a blank screen while
    #: the agent works — which is the thing this feature exists to remove.
    agent_progress: bool = True
    #: A file naming the machines the agent may `ssh` to, and where the code is on them.
    #:
    #: The agent drives a remote coding agent the way its owner does — `ssh box 'cd repo && claude
    #: -p "…"'` — so what it needs is not a tool but *the same knowledge a person would have*. This
    #: is where that knowledge goes, and it is a file rather than a setting because it is prose
    #: about somebody's infrastructure, not a value to parse. `None` means "look for `MACHINES.md`
    #: in the workspace": the agent can read it, and so can you, with `ls`.
    agent_machines_file: Path | None = None
    #: Where the private keys for authorized hosts are kept (docs/design.md §24). `None` means
    #: `$HOME/.ssh` — which under systemd is the agent's own home, the directory `ssh` already
    #: looks in, so a key the agent is meant to use needs no configuration to be findable.
    #:
    #: The keys never travel through the API: this is the directory the *agent* reads, and the
    #: database only ever holds a path.
    agent_key_dir: Path | None = None

    @property
    def resolved_machines_file(self) -> Path | None:
        """The machines file to read, or `None` when the agent has nowhere to keep one."""
        if self.agent_machines_file is not None:
            return self.agent_machines_file
        if self.pi_workspace is not None:
            return self.pi_workspace / "MACHINES.md"
        return None

    @property
    def resolved_registry_file(self) -> Path | None:
        """The **generated** machines file: what is registered, and which hosts are authorized.

        A different file from `MACHINES.md` on purpose. That one is the operator's prose, hand
        written and never touched by this service; this one is rewritten whenever the registry
        or an approval changes, so a separate name is what keeps "do not edit" meaningful.
        """
        if self.pi_workspace is not None:
            return self.pi_workspace / "MACHINES.registered.md"
        return None

    @property
    def resolved_agent_key_dir(self) -> Path:
        """Always a path: keys have to go somewhere, and `$HOME/.ssh` is where ssh looks."""
        if self.agent_key_dir is not None:
            return self.agent_key_dir
        return Path.home() / ".ssh"

    @property
    def resolved_memory_dir(self) -> Path | None:
        """Where the agent's memory files live, or `None` when there is nowhere to put them.

        Defaulting to a `memory/` inside the agent workspace is the point: the memory then
        follows the agent rather than the database, so `MEMORY.md` sits beside the code it is
        about and can be read with `ls`. `None` only happens when no workspace was configured
        either — and the caller treats that as "memory is off" rather than inventing a location,
        because a memory written somewhere nobody chose is worse than no memory.
        """
        if self.memory_dir is not None:
            return self.memory_dir
        if self.pi_workspace is not None:
            return self.pi_workspace / "memory"
        return None

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Settings":
        token = _first_env("CSD_INGEST_TOKEN", "INGEST_TOKEN")
        if require_token and not token:
            raise ConfigError(
                "CSD_INGEST_TOKEN is not set. The ingest API must not run without "
                "authentication; see .env.example."
            )

        db_path = Path(
            os.environ.get("CSD_DB_PATH", str(_project_root() / "data" / "csd.sqlite3"))
        ).resolve()

        static_raw = os.environ.get("CSD_STATIC_DIR")
        static_dir = Path(static_raw).resolve() if static_raw else None

        cors_raw = os.environ.get("CSD_CORS_ORIGINS", "")
        cors = tuple(o.strip() for o in cors_raw.split(",") if o.strip())

        session_dir_raw = os.environ.get("CSD_PI_SESSION_DIR")
        workspace_raw = os.environ.get("CSD_PI_WORKSPACE")
        memory_dir_raw = os.environ.get("CSD_MEMORY_DIR")
        machines_raw = os.environ.get("CSD_AGENT_MACHINES_FILE")
        key_dir_raw = os.environ.get("CSD_AGENT_KEY_DIR")

        return cls(
            db_path=db_path,
            ingest_token=token or "",
            dashboard_token=_first_env("CSD_DASHBOARD_TOKEN"),
            host=os.environ.get("CSD_HOST", "127.0.0.1"),
            port=int(os.environ.get("CSD_PORT", "8780")),
            static_dir=static_dir,
            stale_after_seconds=int(os.environ.get("CSD_STALE_AFTER_SECONDS", "900")),
            event_payload_max_chars=int(os.environ.get("CSD_PAYLOAD_MAX_CHARS", "8000")),
            cors_origins=cors,
            # Empty rather than a literal name: `registry.DEFAULT_BACKEND` is the single
            # declaration of what a fresh deployment talks to, and `Supervisor` applies it to
            # a falsy value. A literal here is how this line came to say "nanobot" while the
            # dataclass above, `dev.sh` and CLAUDE.md all said "native" — and since the env
            # loader is the one that runs in production, a box that set nothing got nanobot,
            # which then dialled a gateway it had no token for every few seconds forever.
            agent_backend=os.environ.get("CSD_AGENT_BACKEND", ""),
            nanobot_ws_url=os.environ.get(
                "CSD_NANOBOT_WS_URL", "ws://127.0.0.1:8765/"
            ),
            nanobot_client_id=os.environ.get("CSD_NANOBOT_CLIENT_ID", "csd-dashboard"),
            nanobot_token=_first_env("CSD_NANOBOT_TOKEN"),
            openclaw_ws_url=os.environ.get("CSD_OPENCLAW_WS_URL", "ws://127.0.0.1:18789/"),
            openclaw_token=_first_env("CSD_OPENCLAW_TOKEN"),
            openclaw_caps=tuple(
                c.strip()
                for c in os.environ.get("CSD_OPENCLAW_CAPS", "tool-events").split(",")
                if c.strip()
            ),
            hermes_api_url=os.environ.get("CSD_HERMES_API_URL", "http://127.0.0.1:8642"),
            hermes_api_key=_first_env("CSD_HERMES_API_KEY"),
            hermes_model=os.environ.get("CSD_HERMES_MODEL", "hermes-agent"),
            # `shlex.split` rather than a plain `.split()`: the binary is often given as
            # `node /path/to/pi.js`, and a Windows install path can legitimately contain
            # spaces. Quoting decides the split, exactly as it would in a shell.
            pi_bin=tuple(shlex.split(os.environ.get("CSD_PI_BIN", "pi"))),
            pi_args=tuple(shlex.split(os.environ.get("CSD_PI_ARGS", ""))),
            pi_workspace=Path(workspace_raw).resolve() if workspace_raw else None,
            pi_session_dir=(
                Path(session_dir_raw).resolve()
                if session_dir_raw
                else db_path.parent / "pi-sessions"
            ),
            pi_provider=_first_env("CSD_PI_PROVIDER"),
            pi_model=_first_env("CSD_PI_MODEL"),
            pi_tools=tuple(
                t.strip() for t in os.environ.get("CSD_PI_TOOLS", "").split(",") if t.strip()
            ),
            pi_env_keys=tuple(
                k.strip()
                for k in os.environ.get("CSD_PI_ENV_KEYS", DEFAULT_PI_ENV_KEYS).split(",")
                if k.strip()
            ),
            pi_command_timeout=float(os.environ.get("CSD_PI_COMMAND_TIMEOUT", "30")),
            agent_idle_compact_minutes=float(
                os.environ.get("CSD_AGENT_IDLE_COMPACT_MINUTES", "15")
            ),
            agent_idle_compact_check_seconds=max(
                1.0, float(os.environ.get("CSD_AGENT_IDLE_COMPACT_CHECK_SECONDS", "60"))
            ),
            memory_dir=(
                Path(memory_dir_raw).resolve() if memory_dir_raw else None
            ),
            # `0` is a real value here — it means "off" — so this cannot use `_first_env`,
            # which treats any empty-or-missing string as absent. `float()` is also strict on
            # purpose: `CSD_MEMORY_INTERVAL_H=often` should fail loudly at boot rather than
            # silently become a number of hours nobody chose.
            memory_interval_h=float(os.environ.get("CSD_MEMORY_INTERVAL_H", "2")),
            memory_dream_batch=max(1, int(os.environ.get("CSD_MEMORY_DREAM_BATCH", "20"))),
            memory_journal_min_chars=max(
                0, int(os.environ.get("CSD_MEMORY_JOURNAL_MIN_CHARS", "400"))
            ),
            memory_model=_first_env("CSD_MEMORY_MODEL"),
            agent_progress=_flag(os.environ.get("CSD_AGENT_PROGRESS"), default=True),
            agent_machines_file=Path(machines_raw).resolve() if machines_raw else None,
            agent_key_dir=Path(key_dir_raw).resolve() if key_dir_raw else None,
        )


def _flag(raw: str | None, *, default: bool) -> bool:
    """A boolean setting, with the spellings people actually write.

    Same reasoning as `CSD_MEMORY_INTERVAL_H`'s `float()`: `CSD_AGENT_PROGRESS=maybe` should not
    quietly become one of two answers nobody chose. An unset value is the default; anything
    unrecognised is an error at boot, which is the only moment it is cheap to fix.
    """
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{raw!r} is not a boolean (use 1/0, true/false, on/off)")


def _project_root() -> Path:
    # backend/app/config.py -> backend/
    return Path(__file__).resolve().parents[2]


#: Import-time alias so callers can write `from .config import from_env`.
from_env = Settings.from_env
