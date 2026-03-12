import inspect
import json
from pathlib import Path
from typing import Any

from .config import RepoPaths, SCHEMA_VERSION, read_json, utc_now_iso, write_json
from .policy import PolicyEngine
from .tracker import (
    assert_status_only_mutation,
    completion_counts,
    load_feature_list,
    normalize_feature_list_payload,
    save_feature_list,
    update_sub_task_status,
)

TOOL_CATALOG_VERSION = "1.0.0"
VALID_MODES = {"planning", "instant"}


DEFAULT_REVIEW_ARTIFACT = (
    (Path(__file__).resolve().parent / "templates" / "rpi_review.md").read_text(encoding="utf-8").rstrip() + "\n"
)


def _mode_decision_policy() -> str:
    """First tool call for every new user request must be mode decision: choose `planning` or `instant`.

    Evaluate the user's task request for current repository / parent directory based on complexity, estimated efforts, multi-turn changes spanning across multiple files or simple trivial edits/updates.

    - `planning`: Invoke this when user's request is complex, introduces a new functionality, adds independent modules or greatly extends functionality for any existing implementation, relates to architectural decisions or 3+ steps, planning for tasks is required, more context or knowledge for the task execution is required including web search or MCP access.

    - `instant`: Invoke for all simple trivial request that do not require much planning, and can be completed with simple changes.
    """

    return inspect.getdoc(_mode_decision_policy) or ""


def _append_event(paths: RepoPaths, run_id: str, payload: dict[str, Any]) -> None:
    event_file = paths.runs_dir / run_id / "events.jsonl"
    event_file.parent.mkdir(parents=True, exist_ok=True)
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": utc_now_iso(), **payload}, sort_keys=True))
        handle.write("\n")


def _resolve_run_id(paths: RepoPaths, run_id: str | None = None) -> str:
    if run_id:
        return run_id
    state = read_json(paths.state_file, {"last_run_id": None})
    active = state.get("last_run_id")
    if active:
        return str(active)
    return "artifact_updates"


def _tool_specifications() -> list[dict[str, Any]]:
    return [
        {
            "id": "workflow.decide_mode",
            "description": "First tool call. The coding agent must choose planning or instant using the inline mode-decision policy in autoeval.harness_tools.",
            "cli": "autoeval tools decide-mode --repo . --request \"<user request>\" --mode planning|instant",
            "parameters": [
                {"name": "request", "type": "string", "required": True},
                {"name": "mode", "type": "string", "required": True},
                {"name": "run_id", "type": "string", "required": False},
            ],
            "outputs": {
                "type": "object",
                "required": ["mode", "run_id", "source", "reasons", "created_at"],
            },
            "errors": ["empty_request", "invalid_mode", "unsupported_auto_mode"],
        },
        {
            "id": "guardrail.check_command",
            "description": "Validate a terminal command against security and policy guardrails.",
            "cli": "autoeval tools guardrail-check --command <cmd> [--target <pytest_target>]",
            "parameters": [
                {"name": "command", "type": "string", "required": True},
                {"name": "target", "type": "string", "required": False},
                {"name": "no_network", "type": "boolean", "required": False},
            ],
            "outputs": {
                "type": "object",
                "required": ["allowed", "reason", "policy_stage", "runtime_approval_required", "metadata"],
            },
            "errors": ["invalid_command", "invalid_pytest_target"],
        },
        {
            "id": "verifier.sync",
            "description": "Resolve linked pytest targets from verifier.yaml and return them without creating a stored artifact file.",
            "cli": "autoeval verifier sync --repo .",
            "parameters": [],
            "outputs": {
                "type": "object",
                "required": ["ok", "link_count", "target_count", "links", "targets"],
            },
            "errors": ["invalid_verifier_yaml", "invalid_verifier_target"],
        },
        {
            "id": "feature.list_generate",
            "description": "Replace feature_list.json through harness normalization so every sub-task matches the typed template.",
            "cli": "autoeval tools feature-list-generate --repo . --input-json '<json payload>'",
            "parameters": [
                {"name": "input_json", "type": "string", "required": True},
                {"name": "run_id", "type": "string", "required": False},
                {"name": "actor", "type": "string", "required": False},
            ],
            "outputs": {
                "type": "object",
                "required": ["ok", "run_id", "feature_list_file", "sub_task_count", "template"],
            },
            "errors": ["invalid_json", "invalid_feature_list_payload", "missing_verifications"],
        },
        {
            "id": "feature.status_set",
            "description": "Update a sub-task status in feature_list.json (status field only).",
            "cli": "autoeval tools feature-status-set --repo . --task-id <id> --status <true|false>",
            "parameters": [
                {"name": "task_id", "type": "string", "required": True},
                {"name": "status", "type": "boolean", "required": True},
                {"name": "run_id", "type": "string", "required": False},
                {"name": "actor", "type": "string", "required": False},
                {"name": "note", "type": "string", "required": False},
            ],
            "outputs": {
                "type": "object",
                "required": ["ok", "run_id", "task_id", "status", "done_count", "total_count"],
            },
            "errors": ["unknown_task_id", "invalid_status_mutation"],
        },
        {
            "id": "feature.status_get",
            "description": "Get a sub-task status from feature_list.json.",
            "cli": "autoeval tools feature-status-get --repo . --task-id <id>",
            "parameters": [{"name": "task_id", "type": "string", "required": True}],
            "outputs": {
                "type": "object",
                "required": ["task_id", "status", "phase_id", "phase", "verifications"],
            },
            "errors": ["unknown_task_id"],
        },
        {
            "id": "verifier.autocheck",
            "description": "Run linked verifier tests for typed feature verifications and optionally update feature status.",
            "cli": "autoeval tools autocheck --repo . [--run-id <id>] [--selection-mode feature-list|all] [--target <pytest_target>]",
            "parameters": [
                {"name": "run_id", "type": "string", "required": False},
                {"name": "sync_verifier_map", "type": "boolean", "required": False},
                {"name": "update_feature_status", "type": "boolean", "required": False},
                {"name": "selection_mode", "type": "string", "required": False},
                {"name": "target", "type": "string[]", "required": False},
                {"name": "timeout_sec", "type": "integer", "required": False},
            ],
            "outputs": {
                "type": "object",
                "required": ["run_id", "passed", "total_checks", "results", "feature_task_target_refs"],
            },
            "errors": ["invalid_selection_mode", "invalid_timeout", "invalid_verification_binding"],
        },
        {
            "id": "run.status",
            "description": "Read harness run status and completion counters.",
            "cli": "autoeval tools run-status --repo . [--run-id <id>]",
            "parameters": [{"name": "run_id", "type": "string", "required": False}],
            "outputs": {
                "type": "object",
                "required": ["run_id", "provider", "executor_mode", "mode", "done_count", "total_count", "completed"],
            },
            "errors": [],
        },
        {
            "id": "run.eval",
            "description": "Execute eval checks for a run.",
            "cli": "autoeval tools run-eval --repo . [--run-id <id>] [--profile default]",
            "parameters": [
                {"name": "run_id", "type": "string", "required": False},
                {"name": "profile", "type": "string", "required": False},
            ],
            "outputs": {
                "type": "object",
                "required": ["run_id", "profile", "passed", "checks", "summary"],
            },
            "errors": ["missing_run_id"],
        },
        {
            "id": "rpi.append_lesson",
            "description": "Append a lesson pattern in review.md Lessons section.",
            "cli": "autoeval tools append-lesson --repo . --text <lesson>",
            "parameters": [{"name": "text", "type": "string", "required": True}],
            "outputs": {
                "type": "object",
                "required": ["ok", "run_id", "review_file", "section"],
            },
            "errors": ["empty_text"],
        },
        {
            "id": "rpi.append_review",
            "description": "Append final review summary in review.md Review section.",
            "cli": "autoeval tools append-review --repo . --text <summary>",
            "parameters": [{"name": "text", "type": "string", "required": True}],
            "outputs": {
                "type": "object",
                "required": ["ok", "run_id", "review_file", "section"],
            },
            "errors": ["empty_text"],
        },
    ]


def tool_catalog_payload(paths: RepoPaths) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": TOOL_CATALOG_VERSION,
        "generated_at": utc_now_iso(),
        "execution_mode": "harness_only",
        "artifact_paths": {
            "research": str(paths.rpi_dir / "research.md"),
            "implementation": str(paths.rpi_dir / "implementation.md"),
            "plan": str(paths.rpi_dir / "plan.md"),
            "review": str(paths.rpi_dir / "review.md"),
            "feature_list": str(paths.rpi_dir / "feature_list.json"),
            "verifier_yaml": str(paths.verifier_file),
            "tool_calls": str(paths.tool_calls_file),
        },
        "tools": _tool_specifications(),
        "loop": {
            "steps": [
                "call workflow.decide_mode first",
                "choose planning or instant using the inline workflow.decide_mode policy",
                "if mode=instant: skip harness loop and jump directly to coding execution",
                "if mode=planning: continue harness loop",
                "read artifacts + tool catalog",
                "call verifier.sync when you need resolved linked pytest targets from verifier.yaml",
                "check terminal commands with guardrail.check_command",
                "implement changes with coding agent outside harness",
                "run verifier.autocheck",
                "update feature statuses via feature.status_set when needed",
                "read run.status and run.eval",
                "repeat until all feature sub-tasks are passing",
            ]
        },
    }


def write_tool_catalog(paths: RepoPaths) -> dict[str, Any]:
    payload = tool_catalog_payload(paths)
    write_json(paths.tool_calls_file, payload)
    return payload


def ensure_review_artifact(paths: RepoPaths) -> Path:
    review_file = paths.review_file
    if review_file.exists():
        return review_file
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_text(DEFAULT_REVIEW_ARTIFACT, encoding="utf-8")
    return review_file


def _append_under_section(document: str, section_name: str, text: str) -> str:
    lines = document.splitlines()
    heading = f"## {section_name}"

    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([heading, f"- {text}"])
        return "\n".join(lines).rstrip() + "\n"

    insert_index = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            insert_index = index
            break
    lines.insert(insert_index, f"- {text}")
    return "\n".join(lines).rstrip() + "\n"


def append_lesson(paths: RepoPaths, text: str, run_id: str | None = None) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("lesson text cannot be empty")
    review_file = ensure_review_artifact(paths)
    current = review_file.read_text(encoding="utf-8")
    updated = _append_under_section(current, "Lessons", text.strip())
    review_file.write_text(updated, encoding="utf-8")

    active_run = _resolve_run_id(paths, run_id=run_id)
    _append_event(
        paths,
        active_run,
        {
            "type": "tool_append_lesson",
            "run_id": active_run,
            "text": text.strip(),
        },
    )
    return {"ok": True, "run_id": active_run, "review_file": str(review_file), "section": "Lessons"}


def append_review(paths: RepoPaths, text: str, run_id: str | None = None) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("review text cannot be empty")
    review_file = ensure_review_artifact(paths)
    current = review_file.read_text(encoding="utf-8")
    updated = _append_under_section(current, "Review", text.strip())
    review_file.write_text(updated, encoding="utf-8")

    active_run = _resolve_run_id(paths, run_id=run_id)
    _append_event(
        paths,
        active_run,
        {
            "type": "tool_append_review",
            "run_id": active_run,
            "text": text.strip(),
        },
    )
    return {"ok": True, "run_id": active_run, "review_file": str(review_file), "section": "Review"}


def check_command_guardrail(
    command: str,
    *,
    target: str | None = None,
    no_network: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engine = PolicyEngine(no_network=no_network)
    return engine.evaluate_terminal_command(command=command, target=target, metadata=metadata).model_dump()


def _normalize_mode(mode: str) -> str:
    value = mode.strip().lower()
    if value not in VALID_MODES:
        raise ValueError(f"invalid mode '{mode}', expected one of: {sorted(VALID_MODES)}")
    return value

def decide_mode(
    paths: RepoPaths,
    request: str,
    *,
    mode: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    request_text = request.strip()
    if not request_text:
        raise ValueError("request cannot be empty")

    mode_input = mode.strip().lower()
    if mode_input == "auto":
        raise ValueError("mode='auto' is not supported; choose planning or instant using the inline workflow.decide_mode policy")

    selected_mode = _normalize_mode(mode_input)
    reasons = [f"selected by executor using the inline mode-decision policy: {selected_mode}"]
    diagnostics = {
        "decision_policy": "inline",
        "agent_selected": True,
        "auto_selection": False,
    }
    decision_source = "agent_selected"

    active_run = _resolve_run_id(paths, run_id=run_id)
    decision = {
        "schema_version": SCHEMA_VERSION,
        "run_id": active_run,
        "request": request_text,
        "mode": selected_mode,
        "source": decision_source,
        "reasons": reasons,
        "diagnostics": diagnostics,
        "created_at": utc_now_iso(),
    }
    _append_event(
        paths,
        active_run,
        {
            "type": "tool_decide_mode",
            "run_id": active_run,
            "mode": selected_mode,
            "source": decision_source,
            "reasons": reasons,
        },
    )
    return decision


def generate_feature_list(
    paths: RepoPaths,
    input_json: str,
    *,
    run_id: str | None = None,
    actor: str = "coding_agent",
) -> dict[str, Any]:
    try:
        raw_payload = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid feature list json: {exc}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError("feature list payload must be a JSON object")

    normalized = normalize_feature_list_payload(raw_payload)
    feature_file = paths.rpi_dir / "feature_list.json"
    save_feature_list(feature_file, normalized)

    active_run = _resolve_run_id(paths, run_id=run_id)
    _append_event(
        paths,
        active_run,
        {
            "type": "tool_feature_list_generate",
            "run_id": active_run,
            "actor": actor,
            "sub_task_count": len(normalized.get("sub_tasks", [])),
            "template": dict(normalized.get("template", {})),
        },
    )
    return {
        "ok": True,
        "run_id": active_run,
        "feature_list_file": str(feature_file),
        "sub_task_count": len(normalized.get("sub_tasks", [])),
        "template": dict(normalized.get("template", {})),
    }


def set_feature_status(
    paths: RepoPaths,
    task_id: str,
    status: bool,
    *,
    run_id: str | None = None,
    actor: str = "coding_agent",
    note: str = "",
) -> dict[str, Any]:
    feature_file = paths.rpi_dir / "feature_list.json"
    before = load_feature_list(feature_file)
    update_sub_task_status(feature_file, task_id=task_id, status=status)
    after = load_feature_list(feature_file)
    assert_status_only_mutation(before, after)

    done_count, total_count = completion_counts(feature_file)
    active_run = _resolve_run_id(paths, run_id=run_id)
    _append_event(
        paths,
        active_run,
        {
            "type": "tool_feature_status_set",
            "run_id": active_run,
            "task_id": task_id,
            "status": bool(status),
            "actor": actor,
            "note": note,
            "done_count": done_count,
            "total_count": total_count,
        },
    )
    return {
        "ok": True,
        "run_id": active_run,
        "task_id": task_id,
        "status": bool(status),
        "done_count": done_count,
        "total_count": total_count,
    }


def get_feature_status(paths: RepoPaths, task_id: str) -> dict[str, Any]:
    payload = load_feature_list(paths.rpi_dir / "feature_list.json")
    for item in payload["sub_tasks"]:
        if str(item.get("id", "")) == task_id:
            return {
                "task_id": task_id,
                "status": bool(item.get("status", False)),
                "phase_id": str(item.get("phase_id", "")),
                "phase": str(item.get("phase", "")),
                "verifications": list(item["verifications"]),
            }
    raise KeyError(f"unknown sub-task id: {task_id}")
