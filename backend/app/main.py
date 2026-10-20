"""FastAPI app factory.

One module is the whole backend surface:
- `/api/*` — JSON + SSE (see `api/`)
- `/` — the built dashboard, if `CSD_STATIC_DIR` points at a `dist/`

Startup order is deliberate: settings (fail fast on a missing token) → database (create
schema, which needs the resolved path) → broker. Nothing else is global, so tests can call
`create_app()` with their own tmp path and get an isolated instance.
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .agent import endpoints, hosts_view
from .agent.backends import AgentBackend
from .agent.supervisor import Supervisor
from .api import agent as agent_api
from .api import events as events_api
from .api import hosts as hosts_api
from .api import sessions as sessions_api
from .api import stream as stream_api
from .config import ConfigError, Settings, from_env as _load_settings
from .database import Database, init_database
from .stream import AgentBroker, Broker, init_broker

logger = logging.getLogger("csd")


def create_app(
    *,
    settings: Settings | None = None,
    db: Database | None = None,
    broker: Broker | None = None,
    agent_broker: AgentBroker | None = None,
    backend: AgentBackend | None = None,
) -> FastAPI:
    """Build the application.

    Dependencies may be injected (tests) or resolved from the environment (production).
    """
    resolved_settings = settings or _load_settings()
    resolved_db = db or init_database(resolved_settings)
    resolved_broker = broker or init_broker()
    resolved_agent_broker = agent_broker or AgentBroker()

    # Before the supervisor, because a backend built below asks "where am I?" and the answer
    # lives in this table (§25). Loading it after construction would leave the first status
    # render claiming nothing had ever registered.
    endpoints.load(resolved_db)

    supervisor = Supervisor(
        resolved_settings, resolved_db, resolved_agent_broker, backend=backend
    )

    # The machines file the agent reads, written once at boot so a fresh deployment has one
    # from the first prompt rather than from its first host heartbeat. A no-op without a
    # workspace, and never fatal: see `hosts_view.write_registry_file`.
    hosts_view.write_registry_file(
        resolved_db,
        resolved_settings.resolved_registry_file,
        stale_after_seconds=resolved_settings.stale_after_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        static = resolved_settings.static_dir
        logger.info(
            "agenest ready: db=%s host=%s:%s static=%s agent=%s",
            resolved_settings.db_path,
            resolved_settings.host,
            resolved_settings.port,
            static or "<none — API only>",
            # The agent, like every route that acts, needs the access token to exist — so
            # this line says the same thing `/api/agent/*` will answer (`auth.access_header`).
            "enabled" if resolved_settings.dashboard_token else "disabled (no access token)",
        )
        if static and not (static / "index.html").is_file():
            # Not fatal: the API works without the dashboard, but the user should know why
            # `/` 404s instead of getting a silently empty page.
            logger.warning(
                "CSD_STATIC_DIR=%s has no index.html; the dashboard will not be served",
                static,
            )
        if resolved_settings.dashboard_token and resolved_settings.pi_workspace is None:
            # Not fatal — the agent still runs — but a bash-capable agent with no stated
            # working directory will use the server's cwd, which is rarely what was meant.
            logger.warning(
                "the agent is enabled but CSD_PI_WORKSPACE is unset; "
                "pi will use the server's working directory"
            )
        # Started, not awaited to completion: connecting to nanobot must not delay the
        # dashboard, which has nothing to do with the agent and should come up regardless.
        # A backend that is down is reported on the agent page, not by refusing to boot.
        await supervisor.start()
        yield
        # Stop the agent before closing the database: event handlers write to it.
        await supervisor.shutdown()
        resolved_db.close()

    app = FastAPI(
        title="Agenest",
        version="0.1.0",
        summary="Coding-agent sessions, and a channel onto your own agent.",
        description=(
            "Receives session and event reports from a local reporter (`cpctl`) and "
            "serves them to a mobile dashboard over SSE. This service executes nothing "
            "itself and opens no outbound connection: the personal agent it hosts may run "
            "`ssh` from its own shell, with keys the user approved per host (design.md §24)."
        ),
        lifespan=lifespan,
    )

    app.state.settings = resolved_settings
    app.state.db = resolved_db
    app.state.broker = resolved_broker
    app.state.agent_broker = resolved_agent_broker
    app.state.supervisor = supervisor

    if resolved_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_origins),
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["*"],
        )

    # The transcript is the one response that gets big — a native conversation can be hundreds
    # of kilobytes — and the reference deployment is reached over a Funnel where the transfer,
    # not the query, is the cost (215 KB took ~9 s; the same read is 12 ms on the box). Starlette
    # excludes `text/event-stream` from compression by default, which is what makes this safe
    # next to SSE: a compressor buffers exactly the frames that must not be buffered.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.include_router(sessions_api.router)
    app.include_router(events_api.router)
    app.include_router(hosts_api.router)
    app.include_router(stream_api.router)
    app.include_router(agent_api.router)
    # The three routes an agent uses to introduce itself before it has any credential: the
    # onboarding document and the registration call. They declare, they never switch (§25).
    app.include_router(agent_api.open_router)

    _mount_static(app, resolved_settings)
    _add_error_handlers(app)

    return app


def _mount_static(app: FastAPI, settings: Settings) -> None:
    """Serve the built dashboard at `/`.

    Mounted last so it cannot shadow `/api`: every API route was registered first, and
    Starlette matches in registration order. `StaticFiles` resolves each path inside the
    directory, so there is no traversal surface — and the whole directory has to be served,
    not just `/assets`, because the PWA files (`manifest.webmanifest`, `sw.js`, `icons/`)
    sit beside the bundle rather than inside it.

    `.webmanifest` is not in Python's MIME table on every platform, and a browser refuses a
    manifest served as `application/octet-stream`, so register it explicitly.
    """
    static = settings.static_dir
    if static is None or not static.is_dir():
        return
    if not (static / "index.html").is_file():
        return

    mimetypes.add_type("application/manifest+json", ".webmanifest")

    index = static / "index.html"

    @app.get("/", include_in_schema=False)
    async def index_page() -> FileResponse:
        return FileResponse(index)

    app.mount("/", StaticFiles(directory=static), name="static")


def _add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ConfigError)
    async def _config_error(_, exc: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"detail": f"server misconfigured: {exc}"},
        )


def main() -> None:
    """Console entry point: `python -m app.main`.

    Re-raises ConfigError rather than starting an unauthenticated server. A dashboard that
    accepts unauthenticated writes is strictly worse than one that refuses to boot.
    """
    logging.basicConfig(level=logging.INFO)
    try:
        settings = _load_settings()
    except ConfigError as exc:
        raise SystemExit(f"refusing to start: {exc}") from exc

    import uvicorn

    app = create_app(settings=settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
