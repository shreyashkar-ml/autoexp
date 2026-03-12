from pathlib import Path
from typing import Any

from .config import SCHEMA_VERSION, read_json, utc_now_iso, write_json

FEATURE_LIST_TEMPLATE_VERSION = "2.2.0"
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


def normalize_feature_list_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_tasks = payload.get("sub_tasks", [])
    if not isinstance(raw_tasks, list):
        raise ValueError("feature list payload 'sub_tasks' must be a list")

    normalized_tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            continue
        normalized_tasks.append(
            {
                "id": str(item.get("id") or f"sub_task_{index}"),
                "phase_id": str(item.get("phase_id") or f"phase_{index}"),
                "phase": str(item.get("phase") or f"Phase {index}"),
                "sub_task_description": str(item.get("sub_task_description") or f"Execute sub_task_{index}"),
                "verifications": require_verifications(item.get("verifications", []), index=index),
                "status": bool(item.get("status", False)),
            }
        )

    _assert_unique_task_ids(normalized_tasks)

    template = payload.get("template", {})
    if template and not isinstance(template, dict):
        raise ValueError("feature list payload 'template' must be an object")
    version = (
        str(template.get("version"))
        if isinstance(template, dict) and str(template.get("version", "")).strip()
        else FEATURE_LIST_TEMPLATE_VERSION
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "template": {"id": "rpi_feature_list", "version": version},
        "generated_at": str(payload.get("generated_at") or utc_now_iso()),
        "sub_tasks": normalized_tasks,
    }


def _assert_unique_task_ids(tasks: list[dict[str, Any]]) -> None:
    task_ids = [str(item.get("id", "")).strip() for item in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("feature list payload 'sub_tasks' must have unique ids")


def load_feature_list(feature_file: Path) -> dict[str, Any]:
    payload = read_json(
        feature_file,
        {
            "schema_version": SCHEMA_VERSION,
            "template": {"id": "rpi_feature_list", "version": FEATURE_LIST_TEMPLATE_VERSION},
            "sub_tasks": [],
        },
    )
    return normalize_feature_list_payload(payload)


def save_feature_list(feature_file: Path, payload: dict[str, Any]) -> None:
    payload["schema_version"] = SCHEMA_VERSION
    write_json(feature_file, payload)


def _tasks_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in payload["sub_tasks"]:
        task_id = str(item.get("id", ""))
        if task_id:
            result[task_id] = item
    return result


def assert_status_only_mutation(before: dict[str, Any], after: dict[str, Any]) -> None:
    before_ids = [str(item.get("id", "")) for item in before["sub_tasks"]]
    after_ids = [str(item.get("id", "")) for item in after["sub_tasks"]]
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
    for item in payload["sub_tasks"]:
        if str(item.get("id", "")) == task_id:
            item["status"] = bool(status)
            found = True
            break
    if not found:
        raise KeyError(f"unknown sub-task id: {task_id}")
    save_feature_list(feature_file, payload)
    return payload


def all_completed(feature_file: Path) -> bool:
    payload = load_feature_list(feature_file)
    tasks = payload["sub_tasks"]
    return bool(tasks) and all(bool(item.get("status", False)) for item in tasks)


def completion_counts(feature_file: Path) -> tuple[int, int]:
    payload = load_feature_list(feature_file)
    tasks = payload["sub_tasks"]
    done_count = sum(1 for item in tasks if bool(item.get("status", False)))
    return done_count, len(tasks)
