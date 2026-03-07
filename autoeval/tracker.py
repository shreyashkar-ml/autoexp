from pathlib import Path
from typing import Any

from .config import SCHEMA_VERSION, read_json, write_json

IMMUTABLE_FIELDS = ("id", "phase_id", "phase", "sub_task_description", "verifications")


def normalize_verifications(raw_verifications: Any, *, index: int) -> list[dict[str, Any]]:
    if not isinstance(raw_verifications, list):
        raw_verifications = []

    normalized: list[dict[str, Any]] = []
    for item in raw_verifications:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        target = str(item.get("target") or "").strip()
        if not kind or not target:
            continue
        normalized.append(
            {
                "kind": kind,
                "target": target,
                "required": bool(item.get("required", True)),
            }
        )

    return normalized


def require_verifications(raw_verifications: Any, *, index: int) -> list[dict[str, Any]]:
    normalized = normalize_verifications(raw_verifications, index=index)
    if normalized:
        return normalized
    raise ValueError(f"sub_task_{index} must define at least one typed verification binding")


def load_feature_list(feature_file: Path) -> dict[str, Any]:
    return read_json(
        feature_file,
        {
            "schema_version": SCHEMA_VERSION,
            "template": {"id": "rpi_feature_list", "version": "2.2.0"},
            "sub_tasks": [],
        },
    )


def save_feature_list(feature_file: Path, payload: dict[str, Any]) -> None:
    payload["schema_version"] = SCHEMA_VERSION
    write_json(feature_file, payload)


def _tasks_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("sub_tasks", []):
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id", ""))
        if task_id:
            result[task_id] = item
    return result


def assert_status_only_mutation(before: dict[str, Any], after: dict[str, Any]) -> None:
    before_ids = [str(item.get("id", "")) for item in before.get("sub_tasks", []) if isinstance(item, dict)]
    after_ids = [str(item.get("id", "")) for item in after.get("sub_tasks", []) if isinstance(item, dict)]
    if before_ids != after_ids:
        raise ValueError("sub_task ids and order cannot change")

    old_tasks = _tasks_by_id(before)
    new_tasks = _tasks_by_id(after)
    for task_id in before_ids:
        old = old_tasks[task_id]
        new = new_tasks[task_id]
        for field in IMMUTABLE_FIELDS:
            if old.get(field) != new.get(field):
                raise ValueError(f"immutable field changed for {task_id}: {field}")
        old_keys = set(old.keys()) - {"status"}
        new_keys = set(new.keys()) - {"status"}
        if old_keys != new_keys:
            raise ValueError(f"non-status fields changed for {task_id}")


def update_sub_task_status(feature_file: Path, task_id: str, status: bool) -> dict[str, Any]:
    payload = load_feature_list(feature_file)
    found = False
    for item in payload.get("sub_tasks", []):
        if isinstance(item, dict) and str(item.get("id", "")) == task_id:
            item["status"] = bool(status)
            found = True
            break
    if not found:
        raise KeyError(f"unknown sub-task id: {task_id}")
    save_feature_list(feature_file, payload)
    return payload


def all_completed(feature_file: Path) -> bool:
    payload = load_feature_list(feature_file)
    tasks = [item for item in payload.get("sub_tasks", []) if isinstance(item, dict)]
    return bool(tasks) and all(bool(item.get("status", False)) for item in tasks)


def completion_counts(feature_file: Path) -> tuple[int, int]:
    payload = load_feature_list(feature_file)
    tasks = [item for item in payload.get("sub_tasks", []) if isinstance(item, dict)]
    done_count = sum(1 for item in tasks if bool(item.get("status", False)))
    return done_count, len(tasks)
