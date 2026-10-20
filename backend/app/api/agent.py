"""`/api/agent/*` — the agenest surface.

Almost every route here is gated by `access_header`: the same token as a read, but **required
to exist** (403 while `CSD_DASHBOARD_TOKEN` is unset). That is not boilerplate — these endpoints
drive an agent that can read and write files, run commands, and ssh out to other machines. The
agent is nanobot (or a local Pi) rather than this service, but "who can POST here" is still the
same question as "who can make that agent do something". See docs/design.md §17 and §22.

**Three routes are deliberately not gated** (`open_router`): the onboarding document and the
registration call. An agent that has not been handed the user's token still has to be able to
read what to do and say where its door is. Registration only *declares* an address — it changes
nothing the page does — while the call that puts the page on that address stays behind the
credential, because that is the call that decides who receives the user's words (§25).

Failures are mapped to status codes that say which layer broke, because the three failures
look identical from a phone otherwise:

- `503` — the agent is unreachable (nanobot is not running, or its address is wrong)
- `504` — the agent was reachable but did not answer in time
- `502` — the agent answered, and the answer was a refusal

There is **one conversation per agent**, and `/session` is how a client gets it (§19). The old
list-and-create pair is gone; nothing here lets a client make a second conversation, and the
routes that take a `thread_id` still do because the id is what the page holds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..agent import endpoints, native_config, onboard, registry, store
from ..agent.backends import BackendError, BackendUnavailable
from ..agent.memory import MEMORY_FILE, SOUL_FILE, USER_FILE
from ..agent.rpc import PiTimeout
from ..agent.supervisor import Supervisor
from ..auth import access_header
from ..schemas import (
    AgentBackendSwitch,
    AgentDreamOut,
    AgentEndpointIn,
    AgentEndpointListOut,
    AgentEndpointOut,
    AgentMemoryCommitOut,
    AgentMemoryFileOut,
    AgentMemoryOut,
    AgentMessageCreate,
    AgentMessageOut,
    AgentStatusOut,
    AgentSwitchOut,
    AgentThreadDetailOut,
    AgentThreadOut,
    NativeConfigCheckOut,
    NativeConfigOut,
    NativeConfigPatch,
)
from ..stream import AgentBroker, agent_sse_message
from . import deps
from .stream import HEARTBEAT_SECONDS, STREAM_HEADERS

logger = logging.getLogger("csd.agent.api")

router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(access_header)])

#: The routes an agent needs before it has any credential of its own: how to connect, and the
#: one call that says where its door is. Registering is a declaration, not a switch — it cannot
#: change what the page talks to, and the address it names must be loopback — so these are
#: reachable by the agent they describe. Everything that drives the agent, or decides which one
#: receives the user's words, stays in `router` above (§25).
open_router = APIRouter(prefix="/api/agent", tags=["agent"])


def _unknown_thread(thread_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown thread id: {thread_id}"
    )


def _unreachable(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"the agent is not reachable: {exc}",
    )


async def _run(awaitable):
    """Run a supervisor call, mapping its failure modes onto HTTP."""
    try:
        return await awaitable
    except KeyError as exc:
        raise _unknown_thread(str(exc)) from None
    except PiTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        ) from None
    except BackendUnavailable as exc:
        # "The agent is not there." The usual cause is that `nanobot gateway` is not running,
        # which is the single most likely thing to be wrong — so the message names it.
        raise _unreachable(exc) from None
    except BackendError as exc:
        # The backend knows what went wrong far better than this layer does, so the message
        # is passed through verbatim rather than flattened into a status code.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    except OSError as exc:
        raise _unreachable(exc) from None


# --------------------------------------------------------------------------- #
# status + threads
# --------------------------------------------------------------------------- #
@router.get("/status", response_model=AgentStatusOut, summary="What the agent page shows")
def agent_status(settings: deps.SettingsDep, supervisor: deps.SupervisorDep) -> AgentStatusOut:
    backend = supervisor.status()
    return AgentStatusOut(
        backend=backend.name,
        connected=backend.connected,
        detail=backend.detail,
        # From the backend rather than from `settings.pi_provider`: once the panel can change
        # the model (§26), the configured value lives in the stored config with the environment
        # as its fallback, and the header has to report what the next process will use.
        provider=backend.provider,
        model=backend.model,
        # "Which directory is this agent operating on" is the first thing you want to know
        # about an agent with shell access — but it is nanobot's workspace, not ours, so the
        # native backend is the only one that can answer.
        workdir=(
            str(settings.pi_workspace)
            if backend.name == "native" and settings.pi_workspace
            else None
        ),
        tools=backend.tools,
        threads_running=backend.conversations,
        backends=supervisor.available_backends(),
    )


@router.post("/backend", response_model=AgentStatusOut, summary="Switch which agent to use")
async def switch_backend(
    data: AgentBackendSwitch,
    settings: deps.SettingsDep,
    supervisor: deps.SupervisorDep,
) -> AgentStatusOut:
    """Change the active agent at runtime.

    Only affects where *new* conversations go. Existing threads keep the backend they were
    created against — re-pointing them would either fail or, worse, hand one thread's history
    to a different agent, and the transcript on screen would quietly stop being true.
    """
    try:
        await supervisor.switch_backend(data.backend)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None
    except BackendUnavailable as exc:
        # Reported, not fatal: the switch did not happen and the previous backend is still
        # serving, so the page can say why without having lost anything.
        raise _unreachable(exc) from None
    return agent_status(settings, supervisor)


@router.post(
    "/switch",
    response_model=AgentSwitchOut,
    summary="Switch agents and return the conversation to show",
)
async def switch_and_show(
    data: AgentBackendSwitch,
    settings: deps.SettingsDep,
    supervisor: deps.SupervisorDep,
    db: deps.DbDep,
    process: Annotated[bool, Query()] = True,
) -> AgentSwitchOut:
    """The page's switch: the same three calls it used to make, answered in one round trip.

    Changing agents made the client call `/backend`, then `/session`, then `/threads/{id}` —
    each dependent on the last, so they could not be overlapped. On a phone behind the Funnel a
    round trip is about a second, which is why a tap felt like the transcript was slow to sync
    rather than like a request was in flight. This route does the same work in one response:
    switch, resolve **the active backend's** conversation, and return it with its messages.

    The primitives are unchanged and remain the API for everything else. This one is not a
    second answer to "how do I switch" — it is the page's three-step read, composed.
    """
    try:
        await supervisor.switch_backend(data.backend)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None
    except BackendUnavailable as exc:
        raise _unreachable(exc) from None

    session = store.get_or_create_session(db, backend=supervisor.backend_name)
    detail = await _run(supervisor.thread_detail(session.id))
    detail.running = supervisor.is_running(session.id)
    detail.streaming = supervisor.is_streaming(session.id)
    if not process:
        # The page's default view hides tool rows, and they are most of a long transcript's
        # bytes. Sending them anyway is what made a switch take seconds over the Funnel.
        detail.messages = [m for m in detail.messages if m.role != "tool"]
    status_out = agent_status(settings, supervisor)
    return AgentSwitchOut(**status_out.model_dump(), thread=detail)


# --------------------------------------------------------------------------- #
# the local agent's configuration (§26)
#
# Only the agent this service runs itself has a model and credentials that are *ours* to edit.
# A gateway agent's are its own, on its own host, so these routes are named for the local one
# rather than pretending to be a generic per-backend settings API.
# --------------------------------------------------------------------------- #
@router.get(
    "/native/config",
    response_model=NativeConfigOut,
    summary="The local agent's model, credentials and permissions",
)
def get_native_config(supervisor: deps.SupervisorDep) -> NativeConfigOut:
    """No API key in the answer — see `NativeConfigOut`."""
    if supervisor.native_backend() is None:
        raise _no_local_agent()
    return NativeConfigOut(**supervisor.native_config_public())


@router.patch(
    "/native/config",
    response_model=NativeConfigOut,
    summary="Change the local agent's configuration",
)
def patch_native_config(
    data: NativeConfigPatch, supervisor: deps.SupervisorDep
) -> NativeConfigOut:
    """Partial update: omitted fields keep their value, `null` clears one back to the environment.

    The change applies to the **next** process this agent starts — its next message — because
    the command line is built when a process is spawned. Nothing is stopped here: interrupting a
    running turn to change a model would abandon the answer being written, and the answer is the
    reason the page exists (docs/design.md §26).
    """
    if supervisor.native_backend() is None:
        raise _no_local_agent()
    try:
        snapshot = supervisor.update_native_config(data.as_patch())
    except native_config.ConfigError as exc:
        # The message is the explanation ("set a provider before saving an API key"), so it is
        # passed through rather than flattened.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None
    return NativeConfigOut(**snapshot)


@router.post(
    "/native/check",
    response_model=NativeConfigCheckOut,
    summary="Ask Pi whether the configured provider and model resolve",
)
async def check_native_config(supervisor: deps.SupervisorDep) -> NativeConfigCheckOut:
    """A free check: `pi --list-models <pattern>` builds the catalogue and exits.

    It answers "does Pi know what this configuration names" — a typo, a provider that was never
    registered, a model the catalogue does not have. It does **not** prove the key works: that
    takes a request and a model call, which is what the first message already does. Saying which
    of the two this was is the whole value of the route.
    """
    backend = supervisor.native_backend()
    if backend is None:
        raise _no_local_agent()
    check = getattr(backend, "check_config", None)
    if check is None:
        raise _no_local_agent()
    ok, detail = await check()
    snapshot = supervisor.native_config_public()
    return NativeConfigCheckOut(
        ok=ok,
        detail=detail,
        provider=snapshot["effective"]["provider"],
        model=snapshot["effective"]["model"],
    )


def _no_local_agent() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="this deployment has no local agent to configure",
    )


# --------------------------------------------------------------------------- #
# onboarding a personal agent (§25)
#
# The direction is worth restating in the code, because the names are the opposite of what
# they sound like: this service is the *client*, so an agent "connecting itself" means it
# opened a door on its own machine and then told us where that door is. The opening and the
# declaration are the agent's; the switch belongs to the user, and is the gated call below.
# Nothing here starts anything on the agent's side.
# --------------------------------------------------------------------------- #
@open_router.get(
    "/onboard",
    summary="How a personal agent connects itself to this page (Markdown)",
    response_class=PlainTextResponse,
)
def onboard_document(request: Request, settings: deps.SettingsDep) -> PlainTextResponse:
    """The instructions, written for an agent rather than for the person who owns it.

    Served from the backends themselves (`registry.onboard_kinds()`), so a new backend that
    declares how its door opens appears here without this route changing.

    **Open on purpose.** It is a document, not a control: an agent that has not been told the
    user's token cannot be expected to ask for one before it can read how to register. It names
    no secret — the capability it describes is "you may say where you live".
    """
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(
        onboard.render_markdown(settings, registry.onboard_kinds(), base=base),
        media_type="text/markdown; charset=utf-8",
    )


@open_router.get("/onboard.json", summary="The same document, as data")
def onboard_json(request: Request, settings: deps.SettingsDep) -> dict:
    base = str(request.base_url).rstrip("/")
    return onboard.render_json(settings, registry.onboard_kinds(), base=base)


@router.get(
    "/backends",
    response_model=AgentEndpointListOut,
    summary="Agent-operated endpoints that have been registered",
)
def list_endpoints(db: deps.DbDep) -> AgentEndpointListOut:
    rows = db.query(
        "SELECT kind, url, label, token, created_at, updated_at, last_probe_at, last_error "
        "FROM agent_endpoints ORDER BY kind"
    )
    return AgentEndpointListOut(endpoints=[_endpoint_out(row) for row in rows])


@open_router.post(
    "/backends",
    response_model=AgentEndpointOut,
    status_code=status.HTTP_201_CREATED,
    summary="An agent says where it can be reached",
)
async def register_endpoint(
    data: AgentEndpointIn,
    db: deps.DbDep,
    settings: deps.SettingsDep,
    supervisor: deps.SupervisorDep,
) -> AgentEndpointOut:
    """Record a door, then knock on it.

    **Why this one is not behind the token.** It is a declaration, and the only thing it can
    change is the list of doors the *user* may choose from. A switch is a different call, on a
    different route, still gated — so an agent can introduce itself without ever being handed a
    credential, and cannot thereby take over the page. The loopback rule keeps the open route
    from becoming "point the page at somebody else's server" (§25); this route never points
    anything, it only says where to knock.

    Stored **before** the probe, so a door that does not open is still a row the user can see —
    "your agent said it lives at X and did not answer" is the sentence that explains the
    situation, and a rejected registration would leave the page looking as if the agent had
    never spoken up. The verdict comes back in `last_error` rather than as a 4xx, because the
    registration *did* succeed; what failed was the other machine.
    """
    kind = data.kind.strip()
    if not registry.has(kind):
        known = ", ".join(registry.available())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown agent kind {kind!r}; this deployment offers: {known}",
        )
    try:
        url = endpoints.validate_url(data.url)
    except endpoints.EndpointError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # **A door may be declared, but not moved while the page is standing on it.** This route is
    # open, and the active backend reads its address per request — so without this check any
    # local process could re-declare the active kind's URL as its own listener and silently
    # receive every message the user types next, with nothing on screen changing. Re-declaring
    # the *same* address is allowed: that is an agent restarting on its port, not a move.
    existing = endpoints.get(kind)
    if (
        existing is not None
        and existing.url != url
        and supervisor.status().name == kind
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{kind!r} is the active agent; switch the page to another backend before "
                f"moving its address from {existing.url} to {url}"
            ),
        )

    stored = endpoints.put(
        db,
        endpoints.AgentEndpoint(
            kind=kind,
            url=url,
            token=(data.token or None),
            label=(data.label or None),
        ),
    )

    ok, error = await supervisor.probe_backend(kind)
    endpoints.record_probe(db, kind, error=None if ok else error)
    logger.info(
        "agent endpoint registered: %s at %s (%s)",
        kind, url, "reachable" if ok else f"not reachable: {error}",
    )

    row = db.query_one(
        "SELECT kind, url, label, token, created_at, updated_at, last_probe_at, last_error "
        "FROM agent_endpoints WHERE kind = ?",
        (stored.kind,),
    )
    return _endpoint_out(row)


@router.delete(
    "/backends/{kind}",
    response_model=AgentEndpointListOut,
    summary="Forget a registered endpoint",
)
async def remove_endpoint(
    kind: str,
    db: deps.DbDep,
    supervisor: deps.SupervisorDep,
) -> AgentEndpointListOut:
    """Refused while it is the active backend.

    Deleting the address out from under the page would leave it pointed at something that just
    stopped existing — a broken screen instead of a message. Switching away first is one call
    the agent already has.
    """
    active = supervisor.status().name
    if active == kind:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{kind!r} is the active agent; switch to another backend before removing its "
                "endpoint"
            ),
        )
    if not endpoints.remove(db, kind):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no registered endpoint for {kind!r}",
        )
    return list_endpoints(db)


def _endpoint_out(row) -> AgentEndpointOut:
    """A row as the API describes it. `has_token`, never the token — see §25."""
    return AgentEndpointOut(
        kind=row["kind"],
        url=row["url"],
        label=row["label"],
        has_token=bool(row["token"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_probe_at=row["last_probe_at"],
        last_error=row["last_error"],
    )


@router.post("/session", response_model=AgentThreadOut, summary="The one conversation")
def open_session(supervisor: deps.SupervisorDep, db: deps.DbDep) -> AgentThreadOut:
    """Get the conversation, creating it on first use.

    There is exactly one session per agent and no way to make a second — the agent's memory
    lives outside the conversation (docs/design.md §18), so a second one would be a second,
    emptier room rather than more space. This replaces the old list/create pair, whose whole
    purpose was letting the client choose among conversations it then had to track.

    **Scoped to the active backend, deliberately.** A thread records which agent it belongs to
    and keeps talking to that one (§17), so returning the globally newest thread after a
    switch would show one agent's transcript while routing the next message to another.

    `POST` rather than `GET` because on a fresh install this really does write a row, and
    calling that a read would be a lie about a route a client might cache. It is idempotent in
    the way that matters: the second call returns the same conversation and changes nothing —
    including when the two calls are concurrent, which is why the store serialises it.
    """
    session = store.get_or_create_session(db, backend=supervisor.backend_name)
    session.running = supervisor.is_running(session.id)
    session.streaming = supervisor.is_streaming(session.id)
    return session


@router.get(
    "/threads/{thread_id}",
    response_model=AgentThreadDetailOut,
    summary="One conversation with its messages",
)
async def get_thread(
    thread_id: str,
    supervisor: deps.SupervisorDep,
    process: Annotated[bool, Query()] = True,
) -> AgentThreadDetailOut:
    """The thread's metadata plus the conversation itself.

    The two halves come from different places and that is the point: the title and the model are
    ours (`agent_threads`), the messages belong to the agent. This route is where they are joined,
    so nothing below it has to hold a copy.

    **`?process=false` omits tool rows**, which in a long native conversation are about nine
    tenths of the bytes — and a client that has turned the process view off is downloading rows
    it will not draw. It is a transport choice, not a different history: the same data is one
    query parameter away.

    **Async, and it asks the backend.** For `native` that is a read of Pi's session file, not
    an RPC round trip: the transcript is already on disk, and starting a Node process just to
    read it would make the page depend on the agent being up. `message_limit` is gone with the
    table that needed it: what comes back is the conversation, as the agent has it.
    """
    detail = await _run(supervisor.thread_detail(thread_id))
    detail.running = supervisor.is_running(thread_id)
    detail.streaming = supervisor.is_streaming(thread_id)
    if not process:
        detail.messages = [m for m in detail.messages if m.role != "tool"]
    return detail


# --------------------------------------------------------------------------- #
# driving the agent
# --------------------------------------------------------------------------- #
@router.post(
    "/threads/{thread_id}/messages",
    response_model=AgentMessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a prompt",
)
async def send_message(
    thread_id: str,
    data: AgentMessageCreate,
    store_db: deps.DbDep,
    supervisor: deps.SupervisorDep,
) -> AgentMessageOut:
    """202, not 201: the prompt has been *accepted*, not answered.

    The reply arrives on `/api/agent/stream` and lands in the thread as it completes. A caller
    that waited for 200 here would hold a connection open for the whole run.
    """
    if store.get_thread_row(store_db, thread_id) is None:
        raise _unknown_thread(thread_id)
    return await _run(supervisor.prompt(thread_id, data.content))


@router.post(
    "/threads/{thread_id}/abort",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stop the current run",
)
async def abort(thread_id: str, db: deps.DbDep, supervisor: deps.SupervisorDep) -> dict:
    if store.get_thread_row(db, thread_id) is None:
        raise _unknown_thread(thread_id)
    # Not an error when nothing is running: abort is a request to stop, and a thread that is
    # already stopped has satisfied it. Reported honestly rather than as a 409.
    return {"aborted": await _run(supervisor.abort(thread_id))}


# --------------------------------------------------------------------------- #
# the stream
# --------------------------------------------------------------------------- #
@router.get("/stream", summary="Agent events")
async def stream(
    request: Request,
    broker: deps.AgentBrokerDep,
    thread_id: Annotated[str | None, Query(max_length=64)] = None,
) -> StreamingResponse:
    """Server-sent events from the agent, **without a cursor**.

    Unlike `/api/events`, there is no `?cursor=` and no `id:` on the frames. Those exist on
    the dashboard stream because every frame there is a database row and can be resumed
    precisely. Here most frames are streaming deltas that are never stored, so a cursor would
    be a promise the server cannot keep. The client's contract is the opposite one: treat
    this stream as *"something changed, and here is the live text"*, and re-fetch the thread
    from `/api/agent/threads/{id}` whenever the connection opens. See `stream.AgentBroker`.

    A client should therefore **open this stream before fetching the thread**. Anything that
    completes in between is then delivered live and also present in the fetch — a duplicate
    the client can drop, which is the harmless direction (design.md §5 makes the same call).
    """
    return StreamingResponse(
        _agent_stream(request=request, broker=broker, thread_id=thread_id),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


async def _agent_stream(
    *, request: Request, broker: AgentBroker, thread_id: str | None
) -> AsyncIterator[str]:
    # Same reason as the dashboard stream: without a leading comment the generator does not
    # yield, response headers are never flushed, and the client sits in CONNECTING with no
    # signal about whether the stream is alive.
    yield ": agent stream open\n\n"

    queue = broker.subscribe()
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            if thread_id is not None and frame.payload.get("thread_id") != thread_id:
                continue
            yield agent_sse_message(frame)
    finally:
        broker.unsubscribe(queue)


# --------------------------------------------------------------------------- #
# memory (docs/design.md §18)
# --------------------------------------------------------------------------- #
@router.get("/memory", response_model=AgentMemoryOut, summary="The agent's long-term memory")
def agent_memory(supervisor: deps.SupervisorDep, db: deps.DbDep) -> AgentMemoryOut:
    """What the memory page renders: the files, the journal's backlog, and the audit trail.

    **File contents are not returned.** These files are the agent's working memory of the user,
    and a route that ships them wholesale is a route that leaks them to anyone holding the page
    token. Names and sizes are what the page needs in order to say memory is on and point at the
    directory; reading the files is `cat`, which is the entire reason they are files.
    """
    journal = supervisor.journal
    directory = supervisor.memory_directory
    if journal is None or directory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this deployment has no memory directory; set CSD_PI_WORKSPACE or CSD_MEMORY_DIR",
        )

    files: list[AgentMemoryFileOut] = []
    for name in (MEMORY_FILE, USER_FILE, SOUL_FILE):
        path = directory / name
        try:
            chars = len(path.read_text(encoding="utf-8"))
            files.append(AgentMemoryFileOut(name=name, present=True, chars=chars))
        except FileNotFoundError:
            files.append(AgentMemoryFileOut(name=name, present=False, chars=0))
        except OSError as exc:
            # Reported as absent rather than failing the whole route: one unreadable file is not
            # a reason to hide the other two, or the journal, or the history.
            logger.warning("could not read %s: %s", path, exc)
            files.append(AgentMemoryFileOut(name=name, present=False, chars=0))

    entries = journal.read()
    pending = supervisor.memory.pending_count() if supervisor.memory is not None else 0
    history = supervisor.memory.git.log(limit=10) if supervisor.memory is not None else []

    return AgentMemoryOut(
        directory=str(directory),
        pending_entries=pending,
        journal_entries=len(entries),
        files=files,
        history=[AgentMemoryCommitOut(**commit) for commit in history],
        interval_hours=supervisor.memory_interval_hours,
    )


@router.post(
    "/memory/dream",
    response_model=AgentDreamOut,
    summary="Consolidate the journal now",
)
async def run_dream(supervisor: deps.SupervisorDep) -> AgentDreamOut:
    """Run Dream on demand.

    The manual counterpart of the interval in `CSD_MEMORY_INTERVAL_H`, and the reason that
    setting can default to off: the schedule is a convenience, not the only way in. A Dream that
    crashes still answers `200` — the run happened, and `advanced: false` says what it did, which
    is more useful than a status code that collapses "nothing to do", "already current" and
    "the model call failed" into one failure.
    """
    if supervisor.memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory is not enabled on this deployment",
        )
    result = await supervisor.run_dream()
    return AgentDreamOut(
        ran=result.ran,
        changed=result.changed,
        advanced=result.advanced,
        detail=result.detail,
        commit=result.commit,
    )


__all__ = ["open_router", "router"]
