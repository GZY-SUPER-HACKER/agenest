"""The native agent's runtime configuration, stored in `meta`.

Every value here was an environment variable read once at startup, so changing a model or a
key meant editing `csd.env` on the box and restarting the service — which is exactly the trip
this product exists to remove. The page is supposed to answer "what is my agent doing" and
"make it do something else" without SSH.

Three rules decide the shape:

- **The database wins; the environment is the fallback.** A cleared field falls back to
  `CSD_PI_PROVIDER` and friends rather than to nothing, so an operator who configured the
  service in a unit file is not silently overruled by an empty form, and "reset" is a real
  action rather than a blank screen.
- **The API key goes in and never comes out.** `public()` reports whether a key is stored and
  which environment variables hold one; no path returns the value. `agent_endpoints.token`
  follows the same rule (§25) for the same reason.
- **The generated provider file carries no secret.** Pi needs a base URL and a model
  catalogue, not the credential; the key travels in the child's environment, which another
  user on the box cannot read, while argv is world-readable.

A config this module cannot parse is logged and treated as empty rather than raised: a bad
row must not stop the agent page from opening, and the empty config is exactly the
environment-backed behaviour that worked before the row existed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from ..database import Database

logger = logging.getLogger("csd.agent.native_config")

#: The `meta` row. A single JSON object rather than one row per field: the config is read and
#: written as a whole, and a partial write that lands between provider and model would leave
#: the agent with half a setup.
META_KEY = "native_config"

#: Pi's built-in tool names. The allowlist is checked against this set rather than passed
#: through: a typo would otherwise silently produce an agent with *no* usable tools, which
#: reads as "the agent is broken" rather than "that name does not exist".
TOOL_NAMES = ("read", "grep", "find", "ls", "bash", "edit", "write")

#: What a custom endpoint is speaking. The three that cover the practical cases; Pi accepts
#: more (`KnownApi` in `@earendil-works/pi-ai`), and adding one here is the whole change.
API_KINDS = ("openai-completions", "openai-responses", "anthropic-messages")
DEFAULT_API_KIND = API_KINDS[0]

#: Used when a context window was given without a max output. Pi requires both to define a
#: model, and a value the user did not choose is better than refusing the save.
DEFAULT_MAX_TOKENS = 8192

MAX_PROVIDER_CHARS = 64
MAX_MODEL_CHARS = 200
MAX_URL_CHARS = 500
MAX_KEY_CHARS = 500
MIN_CONTEXT_WINDOW = 1_000
MAX_CONTEXT_WINDOW = 10_000_000
MAX_OUTPUT_TOKENS = 2_000_000

#: The Pi extension that turns `provider_payload` into a registered provider.
PROVIDER_EXTENSION = Path(__file__).resolve().parent / "pi" / "provider.ts"

#: The child environment variable carrying the API key. A variable rather than `--api-key`
#: because argv is readable by every user on the box while a process's environment is not.
PROVIDER_KEY_ENV = "CSD_AGENT_PROVIDER_KEY"

#: Where the generated provider file's path is announced to the extension.
PROVIDER_CONFIG_ENV = "CSD_AGENT_PROVIDER_CONFIG"


def provider_config_path(settings: Settings) -> Path:
    """Where the generated provider file lives: beside the database, not in the workspace.

    The workspace belongs to the agent; a file this service rewrites on every save would be one
    more thing an agent with `write` could break while editing its own project. Kept here so
    the agent backend and Dream write the same path without one importing the other.
    """
    return settings.db_path.parent / "agent" / "provider.json"


class ConfigError(ValueError):
    """The submitted configuration is not usable. The API maps this to 400."""


@dataclass(frozen=True)
class NativeConfig:
    """What the page has said about the native agent. `None` everywhere means "not set"."""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    #: The credential itself. Present in this dataclass so the child environment can carry it;
    #: `public()` is what the API returns, and it never includes this field.
    api_key: str | None = None
    api_kind: str | None = None
    #: A context window is also the switch that says "define this model here". Empty means Pi's
    #: own catalogue, which is the right default: it knows far more about a built-in model than
    #: a form can be expected to restate.
    context_window: int | None = None
    max_tokens: int | None = None
    #: `None` means Pi's default (every built-in tool, `bash` included). A tuple is an explicit
    #: allowlist. Empty is rejected: "no tools" is a different, deliberate choice Pi spells
    #: `--no-tools`, and it is not offered here.
    tools: tuple[str, ...] | None = None
    #: Pass `--approve`, letting the workspace's own `.pi` resources and extensions load. Off by
    #: default because a workspace is often just a directory of code, not a trusted project.
    trust_project_files: bool = False

    def effective(self, settings: Settings) -> "Effective":
        """This configuration with the environment filling in every field it owns."""
        return Effective(
            provider=self.provider or settings.pi_provider,
            model=self.model or settings.pi_model,
            base_url=self.base_url,
            api_key=self.api_key,
            api_kind=self.api_kind,
            context_window=self.context_window,
            max_tokens=self.max_tokens,
            tools=self.tools if self.tools is not None else (tuple(settings.pi_tools) or None),
            trust_project_files=self.trust_project_files,
        )


EMPTY = NativeConfig()


@dataclass(frozen=True)
class Effective:
    """The configuration after the environment has filled in every field it owns."""

    provider: str | None
    model: str | None
    base_url: str | None
    api_key: str | None
    api_kind: str | None
    context_window: int | None
    max_tokens: int | None
    tools: tuple[str, ...] | None
    trust_project_files: bool

    @property
    def needs_provider_extension(self) -> bool:
        """Whether Pi needs our provider registration at all.

        Only for values the command line cannot carry: a credential we hold, an endpoint that
        is not the provider's own, or a model definition Pi has never heard of. A deployment
        that configures nothing here keeps loading exactly the extensions it loaded before.
        """
        return bool(self.api_key or self.base_url or self.context_window)


# --------------------------------------------------------------------------- #
# reading and writing
# --------------------------------------------------------------------------- #
def load(db: Database) -> NativeConfig:
    """The stored configuration, or `EMPTY` when there is none (or it cannot be read)."""
    try:
        row = db.query_one("SELECT value FROM meta WHERE key = ?", (META_KEY,))
    except Exception:  # a missing table during startup is not a reason to fail the page
        logger.exception("could not read the native agent configuration")
        return EMPTY
    if row is None:
        return EMPTY
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        logger.warning("the stored native agent configuration is not JSON; ignoring it")
        return EMPTY
    if not isinstance(data, dict):
        logger.warning("the stored native agent configuration is not an object; ignoring it")
        return EMPTY
    return _from_dict(data)


def save(db: Database, patch: dict[str, Any], settings: Settings) -> NativeConfig:
    """Apply a partial update and persist it, returning the new configuration.

    `patch` may contain any subset of the fields; a value of `None` clears it. An unknown key
    is an error rather than ignored: a client that misspells `base_url` should hear about it
    instead of watching the save succeed and change nothing.
    """
    merged = _merge(load(db), patch)
    _validate(merged, settings)
    with db.write():
        db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (META_KEY, json.dumps(_to_dict(merged), ensure_ascii=False)),
        )
    return merged


def public(config: NativeConfig, settings: Settings) -> dict[str, Any]:
    """What the settings panel may see. **Never includes the API key.**

    `api_key_env` names (not values) the provider variables present in this service's
    environment: that is how the panel can say "your key comes from the environment" without
    ever holding it, and it is the same shape `agent_endpoints` reports as `has_token`.
    """
    effective = config.effective(settings)
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_kind": config.api_kind,
        "context_window": config.context_window,
        "max_tokens": config.max_tokens,
        "tools": list(config.tools) if config.tools is not None else None,
        "trust_project_files": config.trust_project_files,
        "api_key_set": bool(config.api_key),
        "api_key_env": [
            name for name in settings.pi_env_keys if os.environ.get(name)
        ],
        "workspace": str(settings.pi_workspace) if settings.pi_workspace else None,
        # What the form's placeholders say when a field is empty. The stored value is what the
        # user chose; these are what the service would use instead.
        "env_defaults": {
            "provider": settings.pi_provider,
            "model": settings.pi_model,
            "tools": list(settings.pi_tools) or None,
        },
        "effective": {
            "provider": effective.provider,
            "model": effective.model,
            "tools": list(effective.tools or ()),
        },
    }


def provider_payload(effective: Effective, env_name: str) -> dict[str, Any] | None:
    """The JSON our Pi extension registers, or `None` when it has nothing to say.

    Deliberately credential-free: the extension is told *which environment variable* holds the
    key (`$CSD_AGENT_PROVIDER_KEY`), and Pi resolves it in the child process. Writing the key
    into this file would put it on disk in a second place for no gain.
    """
    if not effective.needs_provider_extension:
        return None
    if not effective.provider:
        # Without a provider name there is nothing to register. Validation refuses this at save
        # time; this guard keeps a hand-edited database from producing a broken extension.
        return None

    payload: dict[str, Any] = {"provider": effective.provider}
    if effective.api_key:
        # Only when we actually hold one. Registering `$CSD_AGENT_PROVIDER_KEY` for a provider
        # whose key comes from the environment would resolve to an empty string and shadow the
        # working credential — a base-URL-only change would then break auth, which is the last
        # thing a networking setting should do.
        payload["apiKeyEnv"] = env_name
    if effective.base_url:
        payload["baseUrl"] = effective.base_url
    if effective.context_window and effective.model:
        payload["api"] = effective.api_kind or DEFAULT_API_KIND
        model_id = _registered_model_id(effective.model, effective.provider)
        payload["models"] = [
            {
                "id": model_id,
                "name": model_id,
                "contextWindow": effective.context_window,
                "maxTokens": effective.max_tokens or DEFAULT_MAX_TOKENS,
            }
        ]
    return payload


def _registered_model_id(model: str, provider: str | None) -> str:
    """The bare id for a model definition: no `provider/` prefix, no `:thinking` suffix.

    Both are Pi conventions on `--model` rather than part of a model's id, and registering
    `deepseek/deepseek-chat` as an id would create a model no request could name.
    """
    value = model
    if provider and value.startswith(f"{provider}/"):
        value = value[len(provider) + 1 :]
    return value.split(":", 1)[0]


# --------------------------------------------------------------------------- #
# merging and validation
# --------------------------------------------------------------------------- #
def _merge(stored: NativeConfig, patch: dict[str, Any]) -> NativeConfig:
    """Apply a patch to a stored config. Unknown keys and impossible values raise."""
    allowed = set(_FIELD_NAMES)
    unknown = set(patch) - allowed
    if unknown:
        raise ConfigError(f"unknown configuration field(s): {', '.join(sorted(unknown))}")

    cleaned: dict[str, Any] = {}
    for name, value in patch.items():
        if value is None:
            # `trust_project_files` is a boolean rather than a nullable field: "clear" is the
            # off position, because there is no environment value for it to fall back to.
            cleaned[name] = False if name == "trust_project_files" else None
            continue
        if name in ("provider", "model", "base_url", "api_key", "api_kind"):
            cleaned[name] = _clean_text(name, value)
        elif name in ("context_window", "max_tokens"):
            cleaned[name] = _clean_int(name, value)
        elif name == "tools":
            cleaned[name] = _clean_tools(value)
        elif name == "trust_project_files":
            cleaned[name] = bool(value)
    return replace(stored, **cleaned)


_FIELD_NAMES = tuple(field.name for field in fields(NativeConfig))


def _clean_text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ConfigError(f"{name} cannot be blank; send null to clear it")
    if name == "provider":
        if len(text) > MAX_PROVIDER_CHARS:
            raise ConfigError(f"provider is longer than {MAX_PROVIDER_CHARS} characters")
        if not all(ch.isalnum() or ch in "._-" for ch in text):
            raise ConfigError("provider may contain letters, digits, '.', '_' and '-' only")
        return text
    if name == "model":
        if len(text) > MAX_MODEL_CHARS:
            raise ConfigError(f"model is longer than {MAX_MODEL_CHARS} characters")
        if any(ch.isspace() for ch in text):
            raise ConfigError("model cannot contain spaces")
        return text
    if name == "base_url":
        if len(text) > MAX_URL_CHARS:
            raise ConfigError(f"base_url is longer than {MAX_URL_CHARS} characters")
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError("base_url must be an http:// or https:// address")
        return text
    if name == "api_kind":
        if text not in API_KINDS:
            raise ConfigError(f"api_kind must be one of {', '.join(API_KINDS)}")
        return text
    if len(text) > MAX_KEY_CHARS:
        raise ConfigError(f"api_key is longer than {MAX_KEY_CHARS} characters")
    return text


def _clean_int(name: str, value: Any) -> int:
    # `bool` is an `int` in Python, and `true` arriving where a number belongs is a client bug
    # that would otherwise become a context window of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be a whole number")
    if name == "context_window":
        if not MIN_CONTEXT_WINDOW <= value <= MAX_CONTEXT_WINDOW:
            raise ConfigError(
                f"context_window must be between {MIN_CONTEXT_WINDOW} and {MAX_CONTEXT_WINDOW}"
            )
        return value
    if not 1 <= value <= MAX_OUTPUT_TOKENS:
        raise ConfigError(f"max_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
    return value


def _clean_tools(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError("tools must be a list of tool names")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError("tools must be a list of tool names")
        name = item.strip()
        if name not in TOOL_NAMES:
            raise ConfigError(
                f"unknown tool {name!r}; the native agent knows {', '.join(TOOL_NAMES)}"
            )
        if name not in names:
            names.append(name)
    if not names:
        raise ConfigError("tools cannot be empty; send null to use Pi's default allowlist")
    return tuple(names)


def _validate(config: NativeConfig, settings: Settings) -> None:
    """Cross-field rules, checked against the environment fallback rather than alone.

    A key with no provider is the one combination that saves fine and then fails at the first
    message with an auth error from a process nobody can see — so it is refused here, where the
    sentence can say what is missing. `settings` is consulted because an empty form still has a
    provider when the operator set one in the unit file.
    """
    effective = config.effective(settings)
    if config.api_key and not effective.provider:
        raise ConfigError("set a provider before saving an API key")
    if config.base_url and not effective.provider:
        raise ConfigError("set a provider before saving a base URL")
    if config.context_window is not None:
        if not effective.provider:
            raise ConfigError("a context window needs a provider")
        if not effective.model:
            raise ConfigError("a context window needs a model id")
    if (
        config.context_window is not None
        and config.max_tokens is not None
        and config.max_tokens > config.context_window
    ):
        raise ConfigError("max_tokens cannot exceed the context window")


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #
def _to_dict(config: NativeConfig) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "api_kind": config.api_kind,
        "context_window": config.context_window,
        "max_tokens": config.max_tokens,
        "tools": list(config.tools) if config.tools is not None else None,
        "trust_project_files": config.trust_project_files,
    }


def _from_dict(data: dict[str, Any]) -> NativeConfig:
    """Read a stored row leniently — a hand-edited database must not break the page.

    The write path is strict (`_merge`); this path is forgiving, because its input is a file
    somebody may have edited at 2 a.m. A value of the wrong type is dropped, which is the
    environment-backed behaviour for that field and therefore safe.
    """
    provider = _stored_text(data.get("provider"), MAX_PROVIDER_CHARS)
    model = _stored_text(data.get("model"), MAX_MODEL_CHARS)
    base_url = _stored_text(data.get("base_url"), MAX_URL_CHARS)
    api_key = _stored_text(data.get("api_key"), MAX_KEY_CHARS)
    api_kind = _stored_text(data.get("api_kind"), 40)
    if api_kind not in API_KINDS:
        api_kind = None
    context_window = _stored_int(data.get("context_window"))
    max_tokens = _stored_int(data.get("max_tokens"))
    tools = data.get("tools")
    parsed_tools = (
        tuple(t for t in tools if isinstance(t, str) and t in TOOL_NAMES)
        if isinstance(tools, list)
        else None
    )
    if parsed_tools == ():
        parsed_tools = None
    return NativeConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        api_kind=api_kind,
        context_window=context_window,
        max_tokens=max_tokens,
        tools=parsed_tools,
        trust_project_files=bool(data.get("trust_project_files")),
    )


def _stored_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _stored_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# --------------------------------------------------------------------------- #
# the merged view
# --------------------------------------------------------------------------- #
__all__ = [
    "API_KINDS",
    "ConfigError",
    "DEFAULT_API_KIND",
    "DEFAULT_MAX_TOKENS",
    "EMPTY",
    "Effective",
    "META_KEY",
    "NativeConfig",
    "PROVIDER_CONFIG_ENV",
    "PROVIDER_EXTENSION",
    "PROVIDER_KEY_ENV",
    "TOOL_NAMES",
    "load",
    "provider_config_path",
    "provider_payload",
    "public",
    "save",
]
