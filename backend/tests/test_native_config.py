"""The local agent's runtime configuration (docs/design.md §26).

Two things are pinned here, and they are the two that would be easy to get wrong later.

**The API key never leaves.** Not in a response, not in the generated provider file, not on a
command line. The tests assert on the serialised bytes rather than on a model field, because the
failure mode is a field somebody added later — `assert "sk-…" not in body` catches that, while
`assert response.api_key is None` does not.

**A save never interrupts a turn.** The change is meant to reach the *next* Pi process: a model
swap is not worth abandoning an answer. The tests drive the two halves of that — the process is
left alone while streaming, and replaced on the next prompt — because a version that passed one
and failed the other would read like a working feature from a phone.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shlex
import sys
from pathlib import Path

import pytest
from app.agent.backends.native import NativeBackend
from app.agent.native_config import (
    PROVIDER_CONFIG_ENV,
    PROVIDER_KEY_ENV,
    NativeConfig,
    provider_config_path,
)
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient
from test_agent_contract import ACCESS_TOKEN, FAKE_PI, _headers


def _settings(tmp_path: Path, **overrides) -> Settings:
    """A `native` deployment with a stub Pi, standing in for the real binary.

    Deliberately its own copy rather than imported from `test_agent_contract`: that helper's
    defaults (`pi_provider="fake"`) are the *environment* this module is testing against, and a
    shared one would make "stored wins over env" pass by accident if either side changed.
    """
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI, encoding="utf-8")
    command = f'"{sys.executable}" "{script}"'
    values = dict(
        db_path=tmp_path / "test.sqlite3",
        ingest_token="test-ingest-token",
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
        dashboard_token=ACCESS_TOKEN,
        agent_backend="native",
        pi_bin=tuple(shlex.split(command)),
        pi_workspace=tmp_path,
        pi_session_dir=tmp_path / "pi-sessions",
        pi_provider="fake",
        pi_model="fake-1",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def agent_app(tmp_path: Path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        yield client, db, settings
    db.close()


def _backend(settings: Settings, config: NativeConfig | None = None) -> NativeBackend:
    return NativeBackend(settings, lambda *_: None, config)


# --------------------------------------------------------------------------- #
# the key never leaves
# --------------------------------------------------------------------------- #
def test_the_api_key_is_never_returned(agent_app) -> None:
    client, _, _ = agent_app
    secret = "sk-do-not-echo-this"

    saved = client.patch(
        "/api/agent/native/config",
        json={"provider": "deepseek", "model": "deepseek-chat", "api_key": secret},
        headers=_headers(),
    )
    assert saved.status_code == 200, saved.text
    assert secret not in saved.text

    read = client.get("/api/agent/native/config", headers=_headers())
    assert secret not in read.text
    assert read.json()["api_key_set"] is True
    # The field must not exist at all, not merely be empty: a JSON `"api_key": null` invites a
    # later change to fill it in.
    assert "api_key" not in read.json()


def test_the_generated_provider_file_carries_no_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config = NativeConfig(
        provider="myproxy",
        model="my-model",
        base_url="https://proxy.example/v1",
        api_key="sk-provider-secret",
        context_window=128_000,
    )
    backend = _backend(settings, config)

    path = provider_config_path(settings)
    text = path.read_text(encoding="utf-8")
    assert "sk-provider-secret" not in text
    payload = json.loads(text)
    assert payload["baseUrl"] == "https://proxy.example/v1"
    assert payload["apiKeyEnv"] == PROVIDER_KEY_ENV
    assert payload["models"][0]["id"] == "my-model"
    assert payload["models"][0]["contextWindow"] == 128_000

    # The credential travels in the child environment instead, and the extension is told where
    # the file is.
    env = backend._child_env()
    assert env[PROVIDER_KEY_ENV] == "sk-provider-secret"
    assert env[PROVIDER_CONFIG_ENV] == str(path)


def test_a_base_url_without_a_stored_key_does_not_shadow_the_environment(
    tmp_path: Path,
) -> None:
    """A networking setting must not break a credential that was already working.

    With no key in the panel, Pi keeps reading the provider's own variable (`DEEPSEEK_API_KEY`
    and friends). Registering `$CSD_AGENT_PROVIDER_KEY` anyway would resolve to an empty string
    and replace that credential with nothing.
    """
    settings = _settings(tmp_path)
    backend = _backend(
        settings,
        NativeConfig(
            provider="deepseek", model="deepseek/deepseek-chat", base_url="https://proxy/v1"
        ),
    )

    payload = json.loads(provider_config_path(settings).read_text(encoding="utf-8"))
    assert payload["baseUrl"] == "https://proxy/v1"
    assert "apiKeyEnv" not in payload
    assert PROVIDER_KEY_ENV not in backend._child_env()


def test_no_provider_extension_without_something_to_register(tmp_path: Path) -> None:
    """A deployment that configures nothing keeps the command line it had before §26."""
    settings = _settings(tmp_path)
    backend = _backend(settings, NativeConfig(provider="deepseek", model="deepseek-chat"))
    argv = backend.build_argv(session_id=None, title="t")
    assert not any(str(arg).endswith("provider.ts") for arg in argv)
    assert not provider_config_path(settings).exists()


def test_the_key_comes_from_the_environment_until_it_is_set_then_cleared(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    settings = _settings(
        tmp_path,
        pi_provider=None,
        pi_model=None,
        pi_env_keys=("DEEPSEEK_API_KEY",),
    )
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    try:
        with TestClient(app) as client:
            before = client.get("/api/agent/native/config", headers=_headers()).json()
            assert before["api_key_set"] is False
            assert before["api_key_env"] == ["DEEPSEEK_API_KEY"]
            assert before["env_defaults"]["provider"] is None

            client.patch(
                "/api/agent/native/config",
                json={"provider": "deepseek", "api_key": "sk-stored"},
                headers=_headers(),
            )
            after = client.get("/api/agent/native/config", headers=_headers()).json()
            assert after["api_key_set"] is True

            # Clearing a field is how you go back to the environment, so it must survive as a
            # `null` in the patch rather than being read as "unchanged".
            client.patch(
                "/api/agent/native/config", json={"api_key": None}, headers=_headers()
            )
            cleared = client.get("/api/agent/native/config", headers=_headers()).json()
            assert cleared["api_key_set"] is False
            assert cleared["api_key_env"] == ["DEEPSEEK_API_KEY"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# the command line
# --------------------------------------------------------------------------- #
def test_the_stored_model_wins_over_the_environment(tmp_path: Path) -> None:
    settings = _settings(tmp_path, pi_provider="fake", pi_model="fake-1")
    backend = _backend(
        settings, NativeConfig(provider="openai", model="openai/gpt-4o-mini")
    )
    argv = backend.build_argv(session_id=None, title="t")
    assert argv[argv.index("--provider") + 1] == "openai"
    assert argv[argv.index("--model") + 1] == "openai/gpt-4o-mini"
    # And what the panel reports is what would be used, not what was typed.
    assert backend.status().provider == "openai"
    assert backend.status().model == "openai/gpt-4o-mini"


def test_an_empty_field_falls_back_to_the_environment(tmp_path: Path) -> None:
    settings = _settings(tmp_path, pi_tools=("read", "grep"))
    argv = _backend(settings, NativeConfig()).build_argv(session_id=None, title="t")
    assert argv[argv.index("--tools") + 1] == "read,grep"


def test_a_tool_allowlist_is_passed_and_full_access_passes_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    read_only = _backend(settings, NativeConfig(tools=("read", "grep", "find", "ls")))
    argv = read_only.build_argv(session_id=None, title="t")
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"

    # `None` is Pi's default — every built-in tool, `bash` included. Passing an empty allowlist
    # would mean "no tools", which is a different and much narrower agent.
    full = _backend(settings, NativeConfig(tools=None))
    assert "--tools" not in full.build_argv(session_id=None, title="t")


def test_trusting_project_files_is_opt_in(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert "--approve" not in _backend(settings).build_argv(session_id=None, title="t")
    trusting = _backend(settings, NativeConfig(trust_project_files=True))
    assert "--approve" in trusting.build_argv(session_id=None, title="t")


# --------------------------------------------------------------------------- #
# when a change takes effect
# --------------------------------------------------------------------------- #
class _StubProcess:
    """A process stand-in that only has to answer "did anything stop me?"."""

    alive = True

    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self.alive = False


def test_a_save_does_not_interrupt_a_running_turn(tmp_path: Path) -> None:
    backend = _backend(_settings(tmp_path))
    stub = _StubProcess()
    backend._procs["c_1"] = stub
    backend._streaming.add("c_1")

    backend.apply_config(NativeConfig(model="another-model"))

    assert stub.stopped is False, "a model swap is not worth abandoning an answer"
    assert "c_1" in backend._restart_pending


def test_the_next_prompt_replaces_the_process(tmp_path: Path) -> None:
    """The other half: the change does land, on the first moment it could matter."""
    backend = _backend(_settings(tmp_path))
    stub = _StubProcess()
    backend._procs["c_1"] = stub
    backend._restart_pending.add("c_1")

    async def run() -> None:
        try:
            proc = await backend._ensure("c_1")
            assert stub.stopped is True
            assert backend._procs["c_1"] is proc
            assert "c_1" not in backend._restart_pending
        finally:
            await backend.stop()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# validation, and the status the page reads
# --------------------------------------------------------------------------- #
def test_a_partial_update_leaves_the_other_fields_alone(agent_app) -> None:
    """Omitted is not the same as null, and the panel depends on the difference.

    The form sends only what it changed, so a model edit must not erase the allowlist — and a
    save that touched nothing must not pin an environment value into the database either.
    """
    client, _, _ = agent_app
    client.patch(
        "/api/agent/native/config",
        json={"provider": "deepseek", "tools": ["read", "grep"]},
        headers=_headers(),
    )
    body = client.patch(
        "/api/agent/native/config", json={"model": "deepseek-reasoner"}, headers=_headers()
    ).json()
    assert body["provider"] == "deepseek"
    assert body["tools"] == ["read", "grep"]
    assert body["model"] == "deepseek-reasoner"


def test_an_unknown_tool_is_refused(agent_app) -> None:
    client, _, _ = agent_app
    response = client.patch(
        "/api/agent/native/config", json={"tools": ["read", "rm"]}, headers=_headers()
    )
    assert response.status_code == 400
    assert "unknown tool" in response.json()["detail"]


def test_a_base_url_that_is_not_http_is_refused(agent_app) -> None:
    client, _, _ = agent_app
    response = client.patch(
        "/api/agent/native/config",
        json={"base_url": "file:///etc/passwd"},
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "http" in response.json()["detail"]


def test_an_api_key_without_a_provider_is_refused(tmp_path: Path) -> None:
    """The one combination that saves cleanly and then fails at the first message."""
    settings = _settings(tmp_path, pi_provider=None, pi_model=None)
    backend = _backend(settings)
    from app.agent import native_config

    db = Database(settings.db_path)
    try:
        with pytest.raises(native_config.ConfigError) as caught:
            native_config.save(db, {"api_key": "sk-1"}, settings)
        assert "provider" in str(caught.value)
    finally:
        db.close()


def test_the_status_reports_the_configured_model(agent_app) -> None:
    client, _, _ = agent_app
    client.patch(
        "/api/agent/native/config",
        json={"provider": "openai", "model": "openai/gpt-4o-mini"},
        headers=_headers(),
    )
    status = client.get("/api/agent/status", headers=_headers()).json()
    assert status["provider"] == "openai"
    assert status["model"] == "openai/gpt-4o-mini"


def test_a_backend_that_cannot_be_configured_has_no_gear(agent_app) -> None:
    """The settings panel draws the gear from the payload, not from a name check in React."""
    client, _, _ = agent_app
    backends = client.get("/api/agent/status", headers=_headers()).json()["backends"]
    native = next(b for b in backends if b["name"] == "native")
    assert native["configurable"] is True
    for other in backends:
        if other["name"] != "native":
            assert other["configurable"] is False


def test_a_stored_configuration_survives_a_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        client.patch(
            "/api/agent/native/config",
            json={"provider": "openai", "model": "openai/gpt-4o"},
            headers=_headers(),
        )
    db.close()

    second = Database(settings.db_path)
    app = create_app(settings=settings, db=second, broker=Broker(), agent_broker=AgentBroker())
    try:
        with TestClient(app) as client:
            config = client.get("/api/agent/native/config", headers=_headers()).json()
            assert config["provider"] == "openai"
            assert config["model"] == "openai/gpt-4o"
    finally:
        second.close()


def test_dream_uses_the_same_provider_as_the_agent(tmp_path: Path) -> None:
    """Otherwise the first Dream after a model change fails auth in a process nobody watches."""
    from app.agent.memory import PiDreamRunner

    settings = dataclasses.replace(_settings(tmp_path), memory_model=None)
    runner = PiDreamRunner(
        settings, config=NativeConfig(provider="openai", model="openai/gpt-4o")
    )
    argv = runner.build_argv(directory=tmp_path)
    assert argv[argv.index("--provider") + 1] == "openai"
    assert argv[argv.index("--model") + 1] == "openai/gpt-4o"
