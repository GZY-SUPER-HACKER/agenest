"""`/api/sessions/{id}/events` — report and read events."""


from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import deps
from ..auth import dashboard_header, token_header
from ..config import Settings
from ..database import Database
from ..schemas import (
    EventAccepted,
    EventCreate,
    EventListOut,
)
from ..services.event_service import UnknownSessionError, ingest_event
from ..services import session_service as ss
from ..stream import Broker

router = APIRouter(tags=["events"])


@router.post(
    "/api/sessions/{session_id}/events",
    response_model=EventAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(token_header)],
    summary="Report one event",
)
def report_event(
    session_id: str,
    data: EventCreate,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    settings: deps.SettingsDep,
) -> EventAccepted:
    """Persist an event, derive session state, then publish to SSE.

    202 rather than 201: the event is durable, but the caller is a fire-and-forget reporter
    that must never block on the dashboard's state. A repeated `event_key` returns the
    original event and re-applies nothing.
    """
    try:
        result = ingest_event(db, session_id, data)
    except UnknownSessionError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown session id: {session_id}; create the session first",
        ) from None

    # Publish only after the transaction has committed. A subscriber must never see an
    # event that a later rollback would take away (spec 6.2).
    # Publish only after the transaction has committed: a subscriber must never see an
    # event that a later rollback could take away (spec 6.2). The frame's sequence number is
    # the database row id, so a client can resume from it even after a service restart.
    broker.publish_event(result.event)

    session = ss.get_session(
        db, session_id, stale_after_seconds=settings.stale_after_seconds
    )
    if session is not None:
        # `get_session` builds a bare row; the frame promises a whole `Session`, and the
        # dashboard reads `last_event` off it to decide whether this session is blocked on a
        # human. Publishing a frame without it would erase that answer on the client.
        ss.attach_last_events(db, [session])
        broker.publish_session(session, after=result.event.id)

    return EventAccepted(session_id=session_id, event=result.event)


@router.get(
    "/api/sessions/{session_id}/events",
    response_model=EventListOut,
    dependencies=[Depends(dashboard_header)],
    summary="Event history, newest first",
)
def list_events(
    session_id: str,
    db: deps.DbDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventListOut:
    if ss.get_session_row(db, session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown session id: {session_id}",
        )
    events, total = ss.list_events(db, session_id, limit=limit, offset=offset)
    return EventListOut(events=events, total=total)


__all__ = ["router"]
