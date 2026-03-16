from pathlib import Path
from typing import Any, Callable

from .config import RepoPaths, SCHEMA_VERSION, ensure_repo_layout, utc_now_iso, write_json
from .harness_tools import write_tool_catalog
from .tracker import (
    FEATURE_LIST_TEMPLATE_VERSION,
    load_feature_list as load_normalized_feature_list,
    normalize_feature_list_payload,
)
from .verifier import ensure_verifier_file, sync_autocheck_map_from_verifier

TEMPLATE_VERSION = FEATURE_LIST_TEMPLATE_VERSION
ARTIFACT_FILES = ("research.md", "implementation.md", "plan.md", "review.md", "feature_list.json")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MARKDOWN_TEMPLATE_FILES = {
    "research.md": "rpi_research.md",
    "implementation.md": "rpi_implementation.md",
    "plan.md": "rpi_plan.md",
    "review.md": "rpi_review.md",
}
ARTIFACT_TEMPLATE_FILES = {**MARKDOWN_TEMPLATE_FILES, "feature_list.json": "rpi_feature_list.md"}


def _load_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8").rstrip() + "\n"


def _artifact_target_file(paths: RepoPaths, artifact_name: str) -> Path:
    if artifact_name == "review.md":
        return paths.review_file
    return paths.rpi_dir / artifact_name


def artifact_instruction_payload(paths: RepoPaths) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for artifact_name in ARTIFACT_FILES:
        target_file = _artifact_target_file(paths, artifact_name)
        if not target_file.exists():
            artifact_state = "missing"
        elif target_file.stat().st_size == 0:
            artifact_state = "empty"
        else:
            artifact_state = "present"
        template_name = ARTIFACT_TEMPLATE_FILES[artifact_name]
        payload.append(
            {
                "artifact_name": artifact_name,
                "target_file": str(target_file),
                "template_file": str(TEMPLATES_DIR / template_name),
                "instructions": _load_template(template_name),
                "artifact_state": artifact_state,
            }
        )
    return payload


def _bootstrap_markdown_artifact(paths: RepoPaths, artifact_name: str) -> str:
    target = _artifact_target_file(paths, artifact_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    return str(target)


def _default_feature_list() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "template": {"id": "rpi_feature_list", "version": TEMPLATE_VERSION},
        "generated_at": utc_now_iso(),
        "sub_tasks": [],
    }


def commit_rpi_artifacts(paths: RepoPaths, payload: dict[str, Any]) -> list[str]:
    ensure_repo_layout(paths)
    written: list[str] = []

    if isinstance(payload.get("research"), str):
        target = paths.rpi_dir / "research.md"
        target.write_text(str(payload["research"]).strip() + "\n", encoding="utf-8")
        written.append(str(target))

    if isinstance(payload.get("implementation"), str):
        target = paths.rpi_dir / "implementation.md"
        target.write_text(str(payload["implementation"]).strip() + "\n", encoding="utf-8")
        written.append(str(target))

    if isinstance(payload.get("plan"), str):
        target = paths.rpi_dir / "plan.md"
        target.write_text(str(payload["plan"]).strip() + "\n", encoding="utf-8")
        written.append(str(target))

    if isinstance(payload.get("review"), str):
        target = paths.review_file
        target.write_text(str(payload["review"]).strip() + "\n", encoding="utf-8")
        written.append(str(target))

    if isinstance(payload.get("feature_list"), dict):
        target = paths.rpi_dir / "feature_list.json"
        write_json(target, normalize_feature_list_payload(payload["feature_list"]))
        written.append(str(target))

    if isinstance(payload.get("tool_calls"), dict):
        write_json(paths.tool_calls_file, payload["tool_calls"])
        written.append(str(paths.tool_calls_file))

    return written


def needs_rpi_bootstrap(paths: RepoPaths) -> bool:
    return not all(_artifact_target_file(paths, name).exists() for name in ARTIFACT_FILES)


def init_rpi_artifacts(
    paths: RepoPaths,
    task: str,
    provider_name: str = "codex",
    force: bool = False,
) -> dict[str, Any]:
    del task, provider_name

    ensure_repo_layout(paths)
    ensure_verifier_file(paths)

    created: list[str] = []
    skipped: list[str] = []

    for artifact_name in MARKDOWN_TEMPLATE_FILES:
        target = _artifact_target_file(paths, artifact_name)
        if force or not target.exists():
            created.append(_bootstrap_markdown_artifact(paths, artifact_name))
        else:
            skipped.append(str(target))

    feature_file = paths.rpi_dir / "feature_list.json"
    if force or not feature_file.exists():
        write_json(feature_file, _default_feature_list())
        created.append(str(feature_file))
    else:
        skipped.append(str(feature_file))

    tool_catalog = write_tool_catalog(paths)
    created.append(str(paths.tool_calls_file))

    sync_result = sync_autocheck_map_from_verifier(paths)
    return {
        "created": created,
        "skipped": skipped,
        "sync": sync_result,
        "tool_catalog": tool_catalog,
        "artifact_instruction_payload": artifact_instruction_payload(paths),
    }


def bootstrap_rpi_with_provider(
    paths: RepoPaths,
    task: str,
    provider_name: str = "codex",
    force: bool = False,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def _status(message: str) -> None:
        if status_callback is None:
            return
        status_callback(message)

    _status("harness_bootstrap_requested")
    outputs = init_rpi_artifacts(paths=paths, task=task, provider_name=provider_name, force=force)
    _status("writing_rpi_artifacts")
    _status("rpi_bootstrap_completed")
    return {
        "ok": True,
        "provider": provider_name,
        "connected": None,
        "provider_connection_error": None,
        "executor_mode": "external_agent",
        "artifacts_written": outputs.get("created", []),
        "sync": outputs.get("sync", {}),
        "artifact_instruction_payload": outputs.get("artifact_instruction_payload", []),
    }


def is_rpi_initialized(paths: RepoPaths) -> bool:
    return all(_artifact_target_file(paths, name).exists() for name in ARTIFACT_FILES)


def load_feature_list(paths: RepoPaths) -> dict[str, Any]:
    return load_normalized_feature_list(paths.rpi_dir / "feature_list.json")
