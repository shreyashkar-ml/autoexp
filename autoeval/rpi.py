from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import RepoPaths, SCHEMA_VERSION, ensure_repo_layout, read_json, utc_now_iso, write_json
from .harness_tools import DEFAULT_REVIEW_ARTIFACT, ensure_review_artifact, write_tool_catalog
from .tracker import require_verifications
from .verifier import ensure_verifier_file, sync_autocheck_map_from_verifier

TEMPLATE_VERSION = "2.2.0"
ARTIFACT_FILES = ("research.md", "implementation.md", "plan.md", "review.md", "feature_list.json", "tool_calls.json")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _load_template(name: str) -> str:
    template_path = TEMPLATES_DIR / name
    return template_path.read_text(encoding="utf-8").rstrip()


def _render_markdown_artifact(name: str, context_lines: list[str]) -> str:
    template_body = _load_template(name)
    context_block = ["## Bootstrap Context", *context_lines]
    return template_body + "\n\n" + "\n".join(context_block) + "\n"


def _research_artifact(task: str) -> str:
    return _render_markdown_artifact(
        "rpi_research.md",
        [
            f"- Requested task: {task}",
            "- Generated during `autoeval init` for repository-level context seeding.",
            "- Use this template to produce repository-specific research, not generic prose.",
        ],
    )


def _implementation_artifact(task: str, provider_name: str) -> str:
    init_file = "CLAUDE.md" if provider_name.strip().lower() in {"claude", "claude-code"} else "AGENTS.md"
    return _render_markdown_artifact(
        "rpi_implementation.md",
        [
            f"- Requested task: {task}",
            f"- Provider: {provider_name}",
            f"- Initialize repository guidance as `{init_file}`",
            "- Express implementation work through typed `verifications`, not free-text criteria.",
        ],
    )


def _plan_artifact(task: str) -> str:
    return _render_markdown_artifact(
        "rpi_plan.md",
        [
            f"- Requested task: {task}",
            "- Reference concrete files/modules when known.",
            "- Define validation per phase before implementation starts.",
        ],
    )


def _default_feature_list() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "template": {"id": "rpi_feature_list", "version": TEMPLATE_VERSION},
        "generated_at": utc_now_iso(),
        "sub_tasks": [],
    }


def _normalize_feature_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_tasks = payload.get("sub_tasks", [])
    if not isinstance(raw_tasks, list):
        raw_tasks = []

    normalized_tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            continue
        normalized_verifications = require_verifications(item.get("verifications", []), index=index)

        normalized_tasks.append(
            {
                "id": str(item.get("id") or f"sub_task_{index}"),
                "phase_id": str(item.get("phase_id") or f"phase_{index}"),
                "phase": str(item.get("phase") or f"Phase {index}"),
                "sub_task_description": str(item.get("sub_task_description") or f"Execute sub_task_{index}"),
                "verifications": normalized_verifications,
                "status": bool(item.get("status", False)),
            }
        )

    template = payload.get("template", {})
    version = (
        str(template.get("version"))
        if isinstance(template, dict) and str(template.get("version", "")).strip()
        else TEMPLATE_VERSION
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "template": {"id": "rpi_feature_list", "version": version},
        "generated_at": str(payload.get("generated_at") or utc_now_iso()),
        "sub_tasks": normalized_tasks,
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
        write_json(target, _normalize_feature_payload(payload["feature_list"]))
        written.append(str(target))

    if isinstance(payload.get("tool_calls"), dict):
        write_json(paths.tool_calls_file, payload["tool_calls"])
        written.append(str(paths.tool_calls_file))

    return written


def needs_rpi_bootstrap(paths: RepoPaths) -> bool:
    return not all((paths.rpi_dir / name).exists() for name in ARTIFACT_FILES)


def init_rpi_artifacts(
    paths: RepoPaths,
    task: str,
    provider_name: str = "codex",
    force: bool = False,
) -> dict[str, Any]:
    ensure_repo_layout(paths)
    ensure_verifier_file(paths)

    created: list[str] = []
    skipped: list[str] = []

    research_file = paths.rpi_dir / "research.md"
    if force or not research_file.exists():
        research_file.write_text(_research_artifact(task), encoding="utf-8")
        created.append(str(research_file))
    else:
        skipped.append(str(research_file))

    implementation_file = paths.rpi_dir / "implementation.md"
    if force or not implementation_file.exists():
        implementation_file.write_text(_implementation_artifact(task, provider_name), encoding="utf-8")
        created.append(str(implementation_file))
    else:
        skipped.append(str(implementation_file))

    plan_file = paths.rpi_dir / "plan.md"
    if force or not plan_file.exists():
        plan_file.write_text(_plan_artifact(task), encoding="utf-8")
        created.append(str(plan_file))
    else:
        skipped.append(str(plan_file))

    feature_file = paths.rpi_dir / "feature_list.json"
    if force or not feature_file.exists():
        write_json(feature_file, _default_feature_list())
        created.append(str(feature_file))
    else:
        skipped.append(str(feature_file))

    review_file = paths.review_file
    if force or not review_file.exists():
        review_file.parent.mkdir(parents=True, exist_ok=True)
        review_file.write_text(DEFAULT_REVIEW_ARTIFACT, encoding="utf-8")
        created.append(str(review_file))
    else:
        ensure_review_artifact(paths)
        skipped.append(str(review_file))

    tool_catalog = write_tool_catalog(paths)
    created.append(str(paths.tool_calls_file))

    sync_result = sync_autocheck_map_from_verifier(paths)
    return {"created": created, "skipped": skipped, "sync": sync_result, "tool_catalog": tool_catalog}


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
        try:
            status_callback(message)
        except Exception:
            return

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
    }


def is_rpi_initialized(paths: RepoPaths) -> bool:
    return all((paths.rpi_dir / name).exists() for name in ARTIFACT_FILES)


def load_feature_list(paths: RepoPaths) -> dict[str, Any]:
    return read_json(paths.rpi_dir / "feature_list.json", _default_feature_list())
