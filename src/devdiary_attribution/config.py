from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from devdiary_attribution.secure_paths import UnsafePathError, reject_symlink_components

DEFAULT_KEY_ENV = "DEVDIARY_INGEST_KEY"
ALLOWED_KINDS = {"agent", "automation"}
ALLOWED_ENFORCEMENT = {"off", "warn", "enforce"}
ALLOWED_MAP_TARGETS = {"session_ref", "provider", "model"}
INGEST_KEY_ENVIRONMENT = re.compile(r"\ADEVDIARY_INGEST_KEY(?:_[A-Z0-9_]+)?\Z")
IDENTITY_NAME = re.compile(r"\A[a-z][a-z0-9_.-]*\Z")
ALLOWED_IDENTITY_TYPES = {
    "git_name",
    "git_email",
    "paperclip_agent_id",
    "hermes_agent_id",
    "claude_code_account_id",
    "codex_account_id",
    "ci_actor_id",
    "cron_job_id",
}
MAP_ENVIRONMENT = {
    "session_ref": re.compile(
        r"\ADEVDIARY_RUNTIME_SESSION_REF(?:_[A-Z0-9]+(?:_[A-Z0-9]+)*)?\Z"
    ),
    "provider": re.compile(
        r"\ADEVDIARY_RUNTIME_PROVIDER(?:_[A-Z0-9]+(?:_[A-Z0-9]+)*)?\Z"
    ),
    "model": re.compile(r"\ADEVDIARY_RUNTIME_MODEL(?:_[A-Z0-9]+(?:_[A-Z0-9]+)*)?\Z"),
}
MAP_ENVIRONMENT_PREFIX = {
    "session_ref": "DEVDIARY_RUNTIME_SESSION_REF",
    "provider": "DEVDIARY_RUNTIME_PROVIDER",
    "model": "DEVDIARY_RUNTIME_MODEL",
}
MAX_REGISTRY_BYTES = 1_048_576


class ConfigError(ValueError):
    """Raised when the editable Cast Registry is invalid."""


def default_config_path(environment: dict[str, str] | None = None) -> Path:
    current_environment = dict(os.environ if environment is None else environment)
    if current_environment.get("DEVDIARY_ATTRIBUTION_CONFIG"):
        return Path(current_environment["DEVDIARY_ATTRIBUTION_CONFIG"]).expanduser()
    if current_environment.get("XDG_CONFIG_HOME"):
        return (
            Path(current_environment["XDG_CONFIG_HOME"]) / "devdiary/attribution.json"
        )
    if os.name == "nt" and current_environment.get("APPDATA"):
        return Path(current_environment["APPDATA"]) / "DevDiary/attribution.json"
    return Path.home() / ".config/devdiary/attribution.json"


def default_registry(
    namespace: str, principal_ref: str, endpoint: str | None
) -> dict[str, Any]:
    registry: dict[str, Any] = {
        "$schema": "urn:devdiary:attribution:schema:v1:registry",
        "version": 1,
        "namespace": namespace,
        "principal_ref": principal_ref,
        "defaults": {
            "enforcement": "warn",
            "anonymous_subagent_policy": "inherit",
        },
        "ingest": {"key_env": DEFAULT_KEY_ENV},
        "actors": [],
        "adapters": {},
    }
    if endpoint:
        registry["ingest"]["url"] = endpoint
    return registry


def write_registry(path: Path, registry: dict[str, Any], force: bool = False) -> None:
    _reject_symlinks(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlinks(path)
    payload = json.dumps(registry, indent=2, sort_keys=True) + "\n"
    if force:
        _replace_registry(path, payload)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ConfigError(f"registry already exists: {path}") from error
    except OSError as error:
        raise ConfigError(f"registry could not be written: {path}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    path.chmod(0o600)


def load_registry(path: Path) -> dict[str, Any]:
    _reject_symlinks(path)
    try:
        if path.stat().st_size > MAX_REGISTRY_BYTES:
            raise ConfigError(f"registry is larger than {MAX_REGISTRY_BYTES} bytes")
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"registry not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"registry is not valid JSON: line {error.lineno}, column {error.colno}"
        ) from error
    validate_registry(registry)
    return registry


def validate_registry(registry: Any) -> None:
    if not isinstance(registry, dict):
        raise ConfigError("registry must be a JSON object")
    if registry.get("version") != 1:
        raise ConfigError("registry version must be 1")
    _known_keys(
        registry,
        {
            "$schema",
            "version",
            "namespace",
            "principal_ref",
            "defaults",
            "ingest",
            "actors",
            "adapters",
        },
        "registry",
    )
    _required_string(registry, "namespace")
    _required_string(registry, "principal_ref")

    defaults = registry.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be an object")
    if defaults.get("enforcement", "warn") not in ALLOWED_ENFORCEMENT:
        raise ConfigError("defaults.enforcement must be off, warn, or enforce")
    if defaults.get("anonymous_subagent_policy", "inherit") not in {
        "inherit",
        "reject",
    }:
        raise ConfigError(
            "defaults.anonymous_subagent_policy must be inherit or reject"
        )
    _known_keys(defaults, {"enforcement", "anonymous_subagent_policy"}, "defaults")

    ingest = registry.get("ingest", {})
    if not isinstance(ingest, dict):
        raise ConfigError("ingest must be an object")
    key_env = ingest.get("key_env", DEFAULT_KEY_ENV)
    if not isinstance(key_env, str) or not INGEST_KEY_ENVIRONMENT.fullmatch(key_env):
        raise ConfigError(
            "ingest.key_env must be DEVDIARY_INGEST_KEY or a namespaced variant"
        )
    _known_keys(ingest, {"url", "key_env"}, "ingest")
    if ingest.get("url") is not None:
        validate_endpoint(ingest["url"])

    actors = registry.get("actors")
    if not isinstance(actors, list):
        raise ConfigError("actors must be an array")
    seen: set[str] = set()
    for actor in actors:
        validate_actor(actor)
        actor_ref = actor["actor_ref"]
        if actor_ref in seen:
            raise ConfigError(f"duplicate actor_ref: {actor_ref}")
        seen.add(actor_ref)

    adapters = registry.get("adapters", {})
    if not isinstance(adapters, dict):
        raise ConfigError("adapters must be an object")
    for name, adapter in adapters.items():
        validate_adapter(name, adapter)


def validate_actor(actor: Any) -> None:
    if not isinstance(actor, dict):
        raise ConfigError("each actor must be an object")
    _known_keys(
        actor,
        {
            "actor_ref",
            "kind",
            "display_name",
            "humanized_name",
            "principal_ref",
            "attester_ref",
            "identities",
            "lane",
            "aliases",
        },
        "actor",
    )
    for field in ("actor_ref", "display_name", "attester_ref"):
        _required_string(actor, field)
    if actor.get("kind") not in ALLOWED_KINDS:
        raise ConfigError("actor.kind must be agent or automation")
    principal_ref = actor.get("principal_ref")
    if principal_ref is not None:
        _validate_text(principal_ref, "actor.principal_ref")
    humanized_name = actor.get("humanized_name")
    if humanized_name is not None:
        _validate_text(humanized_name, "actor.humanized_name")
    lane = actor.get("lane")
    if lane is not None:
        _required_string(actor, "lane")
    aliases = actor.get("aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) for alias in aliases
    ):
        raise ConfigError("actor.aliases must be an array of strings")
    for alias in aliases:
        _validate_text(alias, "actor alias")
    if len(set(aliases)) != len(aliases):
        raise ConfigError("actor.aliases must not contain duplicates")
    identities = actor.get("identities", {})
    if not isinstance(identities, dict):
        raise ConfigError("actor.identities must be an object")
    for name, value in identities.items():
        if not IDENTITY_NAME.fullmatch(name):
            raise ConfigError(f"actor identity type is invalid: {name}")
        if name not in ALLOWED_IDENTITY_TYPES:
            raise ConfigError(f"actor identity type is not allowed: {name}")
        _validate_text(value, f"actor.identities.{name}")


def validate_adapter(name: str, adapter: Any) -> None:
    if not isinstance(name, str) or not IDENTITY_NAME.fullmatch(name):
        raise ConfigError(
            "adapter names must use lowercase letters, numbers, dots, dashes, or underscores"
        )
    if not isinstance(adapter, dict):
        raise ConfigError(f"adapter {name} must be an object")
    _known_keys(adapter, {"execution_role", "runtime", "map"}, f"adapter {name}")
    if adapter.get("execution_role", "executor") not in {"orchestrator", "executor"}:
        raise ConfigError(
            f"adapter {name}.execution_role must be orchestrator or executor"
        )
    runtime = adapter.get("runtime")
    if runtime is not None:
        _validate_text(runtime, f"adapter {name}.runtime")
    mapping = adapter.get("map", {})
    if not isinstance(mapping, dict):
        raise ConfigError(f"adapter {name}.map must be an object")
    if bool(mapping.get("provider")) != bool(mapping.get("model")):
        raise ConfigError(
            f"adapter {name}.map must configure provider and model together"
        )
    for target, source in mapping.items():
        if target not in ALLOWED_MAP_TARGETS:
            raise ConfigError(f"adapter {name} cannot map authoritative field {target}")
        if not isinstance(source, str) or not source.startswith("env:"):
            raise ConfigError(f"adapter {name}.{target} must map from env:VARIABLE")
        variable = source.removeprefix("env:")
        if not MAP_ENVIRONMENT[target].fullmatch(variable):
            raise ConfigError(
                f"adapter {name}.{target} must use a {MAP_ENVIRONMENT_PREFIX[target]} variable"
            )


def validate_endpoint(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ConfigError("ingest.url must be a URL")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ConfigError("ingest.url is malformed") from error
    if not parsed.netloc or not hostname:
        raise ConfigError("ingest.url must include a host")
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ConfigError(
            "ingest.url must use HTTPS (HTTP is allowed only for localhost)"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("ingest.url must not contain user information")
    if parsed.query or parsed.fragment:
        raise ConfigError("ingest.url must not contain a query or fragment")


def find_actor(registry: dict[str, Any], actor_ref: str) -> dict[str, Any]:
    matches = [actor for actor in registry["actors"] if actor["actor_ref"] == actor_ref]
    if not matches:
        raise ConfigError(f"actor not found: {actor_ref}")
    if len(matches) > 1:
        raise ConfigError(f"actor_ref is ambiguous: {actor_ref}")
    return matches[0]


def add_actor(path: Path, actor: dict[str, Any]) -> None:
    registry = load_registry(path)
    validate_actor(actor)
    if any(
        existing["actor_ref"] == actor["actor_ref"] for existing in registry["actors"]
    ):
        raise ConfigError(f"actor already exists: {actor['actor_ref']}")
    registry["actors"].append(actor)
    write_registry(path, registry, force=True)


def add_adapter(path: Path, name: str, adapter: dict[str, Any]) -> None:
    registry = load_registry(path)
    if name in registry["adapters"]:
        raise ConfigError(f"adapter already exists: {name}")
    validate_adapter(name, adapter)
    registry["adapters"][name] = adapter
    write_registry(path, registry, force=True)


def key_environment(registry: dict[str, Any]) -> str:
    return registry.get("ingest", {}).get("key_env", DEFAULT_KEY_ENV)


def endpoint(registry: dict[str, Any]) -> str | None:
    return registry.get("ingest", {}).get("url")


def enforcement(registry: dict[str, Any]) -> str:
    return registry.get("defaults", {}).get("enforcement", "warn")


def _required_string(value: dict[str, Any], field: str) -> None:
    candidate = value.get(field)
    _validate_text(candidate, field)


def _validate_text(candidate: Any, field: str) -> None:
    if not isinstance(candidate, str) or not candidate.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ConfigError(f"{field} must not contain control characters")


def _known_keys(value: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(
            f"{location} contains unsupported fields: {', '.join(unknown)}"
        )


def _reject_symlinks(path: Path) -> None:
    try:
        reject_symlink_components(path)
    except UnsafePathError as error:
        raise ConfigError(str(error)) from error


def _replace_registry(path: Path, payload: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            if hasattr(os, "fchmod"):
                os.fchmod(file.fileno(), 0o600)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        _reject_symlinks(path)
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as error:
        raise ConfigError(f"registry could not be written: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)
