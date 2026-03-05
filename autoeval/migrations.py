from pathlib import Path
from typing import Any

from .config import SCHEMA_VERSION, RepoPaths, ensure_repo_layout, ensure_user_layout, read_json, write_json


def _ensure_schema(path: Path, default_payload: dict[str, Any]) -> None:
    payload = read_json(path, default_payload)
    payload["schema_version"] = SCHEMA_VERSION
    write_json(path, payload)


def run_migrations(paths: RepoPaths) -> None:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)

    _ensure_schema(
        paths.state_file,
        {
            "schema_version": SCHEMA_VERSION,
            "contract_version": "1.0",
            "provider": "codex",
            "last_run_id": None,
        },
    )
    _ensure_schema(paths.project_overrides_file, {"schema_version": SCHEMA_VERSION, "profiles": {}})

    _ensure_schema(paths.user_registry_file, {"schema_version": SCHEMA_VERSION, "profiles": {}})
    _ensure_schema(paths.user_auth_refs_file, {"schema_version": SCHEMA_VERSION, "refs": {}})
    _ensure_schema(paths.user_health_file, {"schema_version": SCHEMA_VERSION, "profiles": {}})

    feature_file = paths.rpi_dir / "feature_list.json"
    if feature_file.exists():
        _ensure_schema(feature_file, {"schema_version": SCHEMA_VERSION, "sub_tasks": []})

    autocheck_map_file = paths.autocheck_map_file
    if autocheck_map_file.exists():
        _ensure_schema(autocheck_map_file, {"schema_version": SCHEMA_VERSION, "links": [], "targets": []})

    tool_calls_file = paths.tool_calls_file
    if tool_calls_file.exists():
        _ensure_schema(tool_calls_file, {"schema_version": SCHEMA_VERSION, "tools": []})
