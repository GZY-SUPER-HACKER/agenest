"""The storage layer's half of the contract: safe for FastAPI's threadpool.

Both rules here come from one production row that sat at `running` for 33 hours with a
`status_changed → completed` event on its timeline (docs/design.md §35). Five `SessionEnd`
hooks fired in the same second; one PATCH was answered 500 (`sqlite3.InterfaceError`, two
requests interleaving on one connection) and another was answered 200 while its `UPDATE` was
rolled back underneath it — the event survived, the status did not.

What makes that class of bug dangerous is the 200: cpctl retries a failure, and it cannot
retry a success. So these tests assert the properties that make a 200 mean what it says.
"""

from __future__ import annotations

import threading
import time

from app.database import Database
from app.schemas import EventCreate, EventType, SessionCreate, SessionPatch, SessionStatus
from app.services import event_service as es
from app.services import session_service as ss


def test_only_one_writer_is_inside_the_database_at_a_time(db: Database) -> None:
    """`write()` serialises writers instead of letting their transactions interleave.

    This is the property that was only claimed by a docstring. Without it the second thread
    is already inside the block before the first has committed, which on a shared connection
    is not merely slow — it is two requests deciding different things about one row.
    """
    active = 0
    peak = 0
    guard = threading.Lock()
    errors: list[BaseException] = []
    writers = 6
    per_writer = 10

    def worker(n: int) -> None:
        nonlocal active, peak
        try:
            for i in range(per_writer):
                with db.write():
                    with guard:
                        active += 1
                        peak = max(peak, active)
                    # Long enough that an unserialised second writer would certainly overlap.
                    time.sleep(0.002)
                    db.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?)",
                        (f"k{n}-{i}", "v"),
                    )
                    with guard:
                        active -= 1
        except BaseException as exc:  # noqa: BLE001 - reported below, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert peak == 1
    rows = db.query_one("SELECT COUNT(*) AS n FROM meta")
    assert rows is not None and rows["n"] == writers * per_writer


def test_a_completion_is_not_reopened_by_racing_events(db: Database) -> None:
    """First-write-wins has to hold against a concurrent ingest, not just a sequential one.

    The event is the dangerous side: it used to write the row's status back after reading it
    before its own transaction began, so a `progress` line that started one millisecond
    earlier than the completion could land `running` on top of it.
    """
    session, _ = ss.create_session(
        db, SessionCreate(agent_type="claude_code", title="Concurrent writes")
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            barrier.wait()
            ss.patch_session(
                db,
                session.id,
                SessionPatch(status=SessionStatus.completed, summary="finished"),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def report() -> None:
        try:
            barrier.wait()
            for i in range(200):
                es.ingest_event(
                    db,
                    session.id,
                    EventCreate(event_type=EventType.progress, message=f"line {i}"),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=complete), threading.Thread(target=report)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    row = ss.get_session_row(db, session.id)
    assert row is not None
    assert row["status"] == "completed"

    event_types = [
        r["event_type"]
        for r in db.query(
            "SELECT event_type FROM session_events WHERE session_id = ?", (session.id,)
        )
    ]
    assert event_types.count("status_changed") == 1
