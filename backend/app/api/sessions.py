"""`/api/sessions` — create, list, read, patch, close.

Auth split (spec 9, 6.1):

- `POST` and `PATCH` require the **ingest token**. Those two are the only endpoints that can
  change state from outside the machine.
- `GET` requires the **dashboard token**, which is only enforced when `CSD_DASHBOARD_TOKEN`
  is set. The deployment already sits behind a Tailscale ACL, so gating reads by default
  would mean typing a token on a phone for no gained security; the guard exists for the
  devices that connect from outside the tailnet.
- `POST /{id}/close` requires the **access token** to be *configured* (403 while it is unset) —
  the same credential as a read, because the page is one page (docs/design.md §22). It is
  deliberately not the ingest token: the dashboard UI is served to anyone who loads the URL, so
  a button in it must not be backed by the credential that writes events. See
  `auth.access_header`.

`PATCH` returning 200 with an unchanged body is deliberate: a refused terminal override and
a de-duplicated retry are both "nothing applied", and neither is an error (spec 6.4).
"""


from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import access_header, dashboard_header, token_header
from ..database import Database
from ..schemas import (
    SessionCreate,
    SessionDetailOut,
    SessionListOut,
    SessionOut,
    SessionPatch,
    SessionStatus,
)
from ..services import session_service as ss
from . import deps

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(token_header)],
    summary="Create a session",
)
def create_session(
    data: SessionCreate,
    db: deps.DbDep,
    broker: deps.BrokerDep,
) -> SessionOut:
    session, start_event = ss.create_session(db, data)

    # Both frames share the `session_started` row id, so a subscriber's cursor advances
    # exactly once for one write. Publish after the transaction has committed.
    broker.publish_event(start_event)
    broker.publish_session(session, after=start_event.id)
    return session


@router.get(
    "",
    response_model=SessionListOut,
    dependencies=[Depends(dashboard_header)],
    summary="List sessions, running first",
)
def list_sessions(
    db: deps.DbDep,
    settings: deps.SettingsDep,
    status_filter: Annotated[SessionStatus | None, Query(alias="status")] = None,
    agent_type: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionListOut:
    sessions, total = ss.list_sessions(
        db,
        status=status_filter,
        agent_type=agent_type,
        limit=limit,
        offset=offset,
        stale_after_seconds=settings.stale_after_seconds,
    )
    return SessionListOut(sessions=sessions, total=total, limit=limit, offset=offset)


@router.get(
    "/{session_id}",
    response_model=SessionDetailOut,
    dependencies=[Depends(dashboard_header)],
    summary="Session detail with its recent events",
)
def get_session(
    db: deps.DbDep,
    settings: deps.SettingsDep,
    session_id: str,
    event_limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> SessionDetailOut:
    detail = ss.get_session_detail(
        db, session_id, stale_after_seconds=settings.stale_after_seconds
    )
    if detail is None:
        raise not_found(session_id)

    events, _total = ss.list_events(db, session_id, limit=event_limit, offset=0)
    detail.events = events
    return detail


@router.patch(
    "/{session_id}",
    response_model=SessionOut,
    dependencies=[Depends(token_header)],
    summary="Update session metadata or status",
)
def patch_session(
    session_id: str,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    data: SessionPatch,
) -> SessionOut:
    session, changed = ss.patch_session(db, session_id, data)
    if session is None:
        raise not_found(session_id)

    if changed:
        # A patch that only touched metadata writes no event of its own, so the SSE frame is
        # anchored to the session's latest event id — a row the client already holds. A
        # refused terminal override applies nothing and therefore says nothing (spec 6.4).
        last = ss.get_last_event_row(db, session_id)
        broker.publish_session(session, after=last["id"] if last else -1)
    return session


def not_found(session_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"unknown session id: {session_id}",
    )


@router.post(
    "/{session_id}/close",
    response_model=SessionOut,
    dependencies=[Depends(access_header)],
    summary="Close a session nothing is going to report on",
)
def close_session(
    session_id: str,
    db: deps.DbDep,
    broker: deps.BrokerDep,
) -> SessionOut:
    """Mark a session `interrupted` on a human's say-so.

    The case this is for: a session created outside the reporting system — a hand-run
    `cpctl session start`, a script, a test fixture that outlived its test — has no owner and
    nothing behind it. No heartbeat will ever probe it (there is no process to look for) and
    no `SessionEnd` will ever fire, so it sits at `running` forever.

    §2 forbids the *server* from resolving that from silence, and rightly: silence looks
    identical whether the work finished, crashed, or was never real. But a person looking at
    the row does know, and this endpoint is how they say so. It records a decision; it does
    not execute anything anywhere, which is what keeps it on the legal side of §1.

    `interrupted`, never `completed`: the honest claim is "this stopped and no outcome was
    reported", which is exactly what `interrupted` means. Guessing an outcome is the thing
    the whole status model exists to avoid.

    Idempotent and safe to repeat: terminal states are sticky and first-write-wins (§3), so
    closing an already-closed session applies nothing and returns the session unchanged.
    """
    data = SessionPatch(
        status=SessionStatus.interrupted,
        summary="Closed from the dashboard — no reporter is going to report an outcome",
    )
    session, changed = ss.patch_session(db, session_id, data)
    if session is None:
        raise not_found(session_id)

    if changed:
        last = ss.get_last_event_row(db, session_id)
        broker.publish_session(session, after=last["id"] if last else -1)
    return session


__all__ = ["router"]
