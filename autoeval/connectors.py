import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import SCHEMA_VERSION, RepoPaths, ensure_repo_layout, ensure_user_layout, read_json, utc_now_iso, write_json

SUPPORTED_TRANSPORTS = {"stdio"}


class MCPProfile(BaseModel):
    name: str
    transport: str = "stdio"
    command: str
    tool_namespace: str
    required_env: list[str] = Field(default_factory=list)
    auth_ref: str | None = None
    timeout_s: int = 60
    enabled: bool = True

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_TRANSPORTS:
            raise ValueError(f"unsupported transport: {value}")
        return normalized

    @field_validator("timeout_s")
    @classmethod
    def validate_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timeout_s must be > 0")
        return value


class MCPOverride(BaseModel):
    enabled: bool | None = None
    transport: str | None = None
    command: str | None = None
    tool_namespace: str | None = None
    required_env: list[str] | None = None
    auth_ref: str | None = None
    timeout_s: int | None = None


def _load_registry(path: Path) -> dict[str, Any]:
    return read_json(path, {"schema_version": SCHEMA_VERSION, "profiles": {}})


def _save_registry(path: Path, payload: dict[str, Any]) -> None:
    payload["schema_version"] = SCHEMA_VERSION
    write_json(path, payload)


def _apply_override(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        merged[key] = value
    return merged


def add_profile(
    paths: RepoPaths,
    scope: str,
    name: str,
    transport: str,
    command: str,
    tool_namespace: str,
    required_env: list[str] | None = None,
    timeout_s: int = 60,
    enabled: bool = True,
) -> dict[str, Any]:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)

    if scope == "user":
        profile = MCPProfile(
            name=name,
            transport=transport,
            command=command,
            tool_namespace=tool_namespace,
            required_env=required_env or [],
            timeout_s=timeout_s,
            enabled=enabled,
        )
        registry = _load_registry(paths.user_registry_file)
        registry.setdefault("profiles", {})[name] = profile.model_dump()
        _save_registry(paths.user_registry_file, registry)
        return registry["profiles"][name]

    if scope == "project":
        override = MCPOverride(
            enabled=enabled,
            transport=transport or None,
            command=command or None,
            tool_namespace=tool_namespace or None,
            required_env=required_env,
            timeout_s=timeout_s,
        )
        registry = _load_registry(paths.project_overrides_file)
        registry.setdefault("profiles", {})[name] = {
            key: value for key, value in override.model_dump().items() if value is not None
        }
        _save_registry(paths.project_overrides_file, registry)
        return registry["profiles"][name]

    raise ValueError("scope must be user or project")


def remove_profile(paths: RepoPaths, scope: str, name: str) -> bool:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")
    registry_file = paths.user_registry_file if scope == "user" else paths.project_overrides_file
    registry = _load_registry(registry_file)
    existed = name in registry.get("profiles", {})
    registry.get("profiles", {}).pop(name, None)
    _save_registry(registry_file, registry)
    return existed


def set_profile_enabled(paths: RepoPaths, scope: str, name: str, enabled: bool) -> dict[str, Any]:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)

    if scope == "user":
        registry = _load_registry(paths.user_registry_file)
        if name not in registry.get("profiles", {}):
            raise KeyError(name)
        registry["profiles"][name]["enabled"] = bool(enabled)
        _save_registry(paths.user_registry_file, registry)
        return registry["profiles"][name]

    if scope == "project":
        registry = _load_registry(paths.project_overrides_file)
        profile = dict(registry.get("profiles", {}).get(name, {}))
        profile["enabled"] = bool(enabled)
        registry.setdefault("profiles", {})[name] = profile
        _save_registry(paths.project_overrides_file, registry)
        return registry["profiles"][name]

    raise ValueError("scope must be user or project")


def set_auth_ref(paths: RepoPaths, name: str, auth_ref: str) -> dict[str, Any]:
    ensure_user_layout(paths)
    refs = read_json(paths.user_auth_refs_file, {"schema_version": SCHEMA_VERSION, "refs": {}})
    refs.setdefault("refs", {})[name] = auth_ref
    refs["schema_version"] = SCHEMA_VERSION
    write_json(paths.user_auth_refs_file, refs)

    registry = _load_registry(paths.user_registry_file)
    if name in registry.get("profiles", {}):
        registry["profiles"][name]["auth_ref"] = auth_ref
        _save_registry(paths.user_registry_file, registry)

    return {"name": name, "auth_ref": auth_ref}


def resolve_effective_profiles(paths: RepoPaths) -> dict[str, dict[str, Any]]:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    user_profiles = _load_registry(paths.user_registry_file).get("profiles", {})
    project_overrides = _load_registry(paths.project_overrides_file).get("profiles", {})

    effective: dict[str, dict[str, Any]] = {}
    for name in sorted(set(user_profiles) | set(project_overrides)):
        if name in user_profiles:
            base = dict(user_profiles[name])
        else:
            base = {
                "name": name,
                "transport": "stdio",
                "command": "",
                "tool_namespace": "",
                "required_env": [],
                "timeout_s": 60,
                "enabled": True,
            }
        if name in project_overrides:
            base = _apply_override(base, project_overrides[name])
        base["name"] = name
        effective[name] = base
    return effective


def _preflight(profile: dict[str, Any], auth_refs: dict[str, str]) -> tuple[bool, str | None]:
    errors: list[str] = []
    try:
        parsed = MCPProfile(**profile)
    except ValidationError as exc:
        return False, str(exc)

    for name in parsed.required_env:
        if os.getenv(name) is None:
            errors.append(f"missing env var: {name}")

    if parsed.auth_ref and auth_refs.get(parsed.name) != parsed.auth_ref:
        errors.append(f"auth_ref mismatch for profile {parsed.name}")

    if errors:
        return False, "; ".join(errors)
    return True, None


def connect_profile(paths: RepoPaths, name: str) -> dict[str, Any]:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)

    effective = resolve_effective_profiles(paths)
    if name not in effective:
        raise KeyError(name)
    refs = read_json(paths.user_auth_refs_file, {"schema_version": SCHEMA_VERSION, "refs": {}})
    auth_refs = refs.get("refs", {})
    ok, error = _preflight(effective[name], auth_refs)

    health = read_json(paths.user_health_file, {"schema_version": SCHEMA_VERSION, "profiles": {}})
    health.setdefault("profiles", {})[name] = {
        "connected": bool(ok),
        "last_checked": utc_now_iso(),
        "last_error": error,
    }
    health["schema_version"] = SCHEMA_VERSION
    write_json(paths.user_health_file, health)

    if not ok:
        raise ValueError(error or "profile preflight failed")
    return health["profiles"][name]


def disconnect_profile(paths: RepoPaths, name: str) -> dict[str, Any]:
    ensure_user_layout(paths)
    health = read_json(paths.user_health_file, {"schema_version": SCHEMA_VERSION, "profiles": {}})
    health.setdefault("profiles", {})[name] = {
        "connected": False,
        "last_checked": utc_now_iso(),
        "last_error": None,
    }
    health["schema_version"] = SCHEMA_VERSION
    write_json(paths.user_health_file, health)
    return health["profiles"][name]


def list_profiles(paths: RepoPaths, scope: str = "effective") -> dict[str, Any]:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    if scope == "user":
        return _load_registry(paths.user_registry_file).get("profiles", {})
    if scope == "project":
        return _load_registry(paths.project_overrides_file).get("profiles", {})
    if scope == "effective":
        effective = resolve_effective_profiles(paths)
        health = read_json(paths.user_health_file, {"schema_version": SCHEMA_VERSION, "profiles": {}})
        for name, profile in effective.items():
            profile["health"] = health.get("profiles", {}).get(name, {})
        return effective
    raise ValueError("scope must be user, project, or effective")


def resolve_runtime_profiles(paths: RepoPaths) -> dict[str, dict[str, Any]]:
    effective = resolve_effective_profiles(paths)
    refs = read_json(paths.user_auth_refs_file, {"schema_version": SCHEMA_VERSION, "refs": {}})
    auth_refs = refs.get("refs", {})

    runtime: dict[str, dict[str, Any]] = {}
    for name, profile in effective.items():
        if not profile.get("enabled", True):
            continue
        ok, _ = _preflight(profile, auth_refs)
        if ok:
            runtime[name] = profile
    return runtime


def map_tool_selector_to_profile(
    paths: RepoPaths,
    selector: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any] | None:
    runtime = resolve_runtime_profiles(paths)
    if namespace:
        for name, profile in runtime.items():
            if name == namespace or profile.get("tool_namespace") == namespace:
                return profile
    if selector:
        for name, profile in runtime.items():
            if name == selector or profile.get("tool_namespace") == selector:
                return profile
    return None
