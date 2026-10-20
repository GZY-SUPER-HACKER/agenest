"""Constants and helpers shared by the test modules.

`conftest.py` fixtures are auto-discovered but its plain names are not, so the things tests
reference directly live here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

TOKEN = "test-ingest-token"
DASH_TOKEN = "dash-token"

INGEST = {"X-CPD-Token": TOKEN}
READ = {"X-CPD-Token": DASH_TOKEN}


def report(
    client: TestClient, session_id: str, event_type: str, message: str, **extra
):
    """Report one event, the way the CLI would."""
    return client.post(
        f"/api/sessions/{session_id}/events",
        json={"event_type": event_type, "message": message, **extra},
        headers=INGEST,
    )
