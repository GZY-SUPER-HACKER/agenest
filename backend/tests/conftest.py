"""Shared fixtures.

Each test gets a fresh SQLite file and its own broker, so tests neither leak state nor
depend on execution order. The app is built with explicit dependencies rather than from the
environment, which is what keeps the tests off the real database and off a real token.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Tests talk to loopback servers they started themselves. On a developer machine with a
#: system proxy (Clash et al.), httpx picks that proxy up through
#: `urllib.request.getproxies()` — on Windows, from the registry — and sends 127.0.0.1 to it.
#: The proxy then answers for a server that is right here, and the SSE tests fail with a read
#: error that has nothing to do with the code under test. The backend's own loopback clients
#: set `trust_env=False` for the same reason; this is the test-side half.
_no_proxy = os.environ.get("NO_PROXY", "")
os.environ["NO_PROXY"] = ",".join(filter(None, [_no_proxy, "127.0.0.1,localhost,::1"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.stream import Broker
from fastapi.testclient import TestClient

TOKEN = "test-ingest-token"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    # No dashboard token by default: the deployment's read surface is already behind a
    # Tailscale ACL, and requiring one by default means typing it on a phone for nothing.
    return Settings(
        db_path=tmp_path / "test.sqlite3",
        ingest_token=TOKEN,
        dashboard_token=None,
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
    )


@pytest.fixture()
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture()
def broker() -> Broker:
    return Broker()


@pytest.fixture()
def client(settings: Settings, db: Database, broker: Broker) -> TestClient:
    app = create_app(settings=settings, db=db, broker=broker)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def session_id(client: TestClient) -> str:
    """A running session, as the dashboard would first see it."""
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "Implement auth", "project_name": "myproj"},
        headers={"X-CPD-Token": TOKEN},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


INGEST = {"X-CPD-Token": TOKEN}
READ = {"X-CPD-Token": "dash-token"}
