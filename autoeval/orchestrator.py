import json
from pathlib import Path
import shutil
from typing import Any

from .config import RepoPaths, SCHEMA_VERSION, ensure_repo_layout, read_json, touch_state, utc_now_iso, write_json
from .connectors import resolve_runtime_profiles
from .evals import EvalCheck, run_eval_suite
from .harness_tools import decide_mode, tool_catalog_payload, write_tool_catalog
from .provider_surface import write_provider_session
from .rpi import init_rpi_artifacts, is_rpi_initialized
from .security import guardrail_summary
from .tracker import all_completed, completion_counts, load_feature_list
from .verifier import mapped_target_ids, run_autocheck

VALID_MODES = {"planning", "instant"}


def _append_event(run_dir: Path, payload: dict[str, Any]) -> None:
    event_file = run_dir / "events.jsonl"
    event_file.parent.mkdir(parents=True, exist_ok=True)
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": utc_now_iso(), **payload}, sort_keys=True))
        handle.write("\n")


def _append_progress(run_dir: Path, text: str) -> None:
    progress_file = run_dir / "progress.md"
    if progress_file.exists():
        with progress_file.open("a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        progress_file.write_text(text, encoding="utf-8")


def _next_run_id() -> str:
    token = utc_now_iso().replace(":", "").replace("-", "").replace("+", "_")
    return f"run_{token}"


def _load_session_meta(run_dir: Path, provider: str) -> dict[str, Any]:
    meta_file = run_dir / "session_meta.json"
    payload = read_json(
        meta_file,
        {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "executor_mode": "external_agent",
            "mode": "planning",
            "session_count": 0,
            "created_at": utc_now_iso(),
        },
    )
    if "session_count" not in payload:
        payload["session_count"] = 0
    return payload


def _write_usage(paths: RepoPaths, run_id: str, session_number: int, checks_run: int) -> dict[str, Any]:
    usage_file = paths.runs_dir / run_id / "usage.json"
    payload = read_json(
        usage_file,
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "sessions": [],
            "totals": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "actions": 0,
                "verifier_checks": 0,
            },
        },
    )
    payload.setdefault("sessions", []).append(
        {
            "session_number": session_number,
            "executor_mode": "external_agent",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
            },
            "actions": 0,
            "verifier_checks": checks_run,
            "created_at": utc_now_iso(),
        }
    )

    totals = payload.setdefault("totals", {})
    totals["input_tokens"] = int(totals.get("input_tokens", 0))
    totals["output_tokens"] = int(totals.get("output_tokens", 0))
    totals["total_tokens"] = int(totals.get("total_tokens", 0))
    totals["estimated_cost_usd"] = float(totals.get("estimated_cost_usd", 0.0))
    totals["actions"] = int(totals.get("actions", 0))
    totals["verifier_checks"] = int(totals.get("verifier_checks", 0)) + checks_run
    payload["schema_version"] = SCHEMA_VERSION
    payload["updated_at"] = utc_now_iso()
    write_json(usage_file, payload)
    return payload


def _build_loop_context_payload(
    paths: RepoPaths,
    run_id: str,
    session_number: int,
    task: str,
    provider: str,
) -> dict[str, Any]:
    feature_payload = load_feature_list(paths.rpi_dir / "feature_list.json")
    sub_tasks = feature_payload["sub_tasks"]
    pending = [item for item in sub_tasks if not bool(item.get("status", False))]
    completed = [item for item in sub_tasks if bool(item.get("status", False))]

    linked_targets = mapped_target_ids(paths)
    runtime_profiles = resolve_runtime_profiles(paths)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "session_number": session_number,
        "provider": provider,
        "executor_mode": "external_agent",
        "mode": "planning",
        "task": task,
        "created_at": utc_now_iso(),
        "artifacts": {
            "research": str(paths.rpi_dir / "research.md"),
            "implementation": str(paths.rpi_dir / "implementation.md"),
            "plan": str(paths.rpi_dir / "plan.md"),
            "review": str(paths.review_file),
            "feature_list": str(paths.rpi_dir / "feature_list.json"),
            "verifier_yaml": str(paths.verifier_file),
            "tool_calls": str(paths.tool_calls_file),
        },
        "summary": {
            "sub_tasks_total": len(sub_tasks),
            "sub_tasks_done": len(completed),
            "sub_tasks_pending": len(pending),
            "linked_verifier_targets": len(linked_targets),
            "runtime_mcp_profiles": sorted(runtime_profiles.keys()),
        },
        "guardrails": guardrail_summary(),
        "tool_catalog": tool_catalog_payload(paths),
        "pending_sub_tasks": [
            {
                "id": str(item.get("id", "")),
                "phase_id": str(item.get("phase_id", "")),
                "phase": str(item.get("phase", "")),
                "sub_task_description": str(item.get("sub_task_description", "")),
                "verifications": [
                    {
                        "kind": str(entry.get("kind", "")),
                        "target": str(entry.get("target", "")),
                        "required": bool(entry.get("required", True)),
                    }
                    for entry in item["verifications"]
                ],
            }
            for item in pending
        ],
        "instructions": [
            "autoeval is harness-only; use tool calls for status/verification/guardrails.",
            "read .autoeval/runtime/tool_calls.json and call tools explicitly in loop.",
            "mutate only feature_list status through harness tool interfaces.",
        ],
    }


def _build_instant_context_payload(paths: RepoPaths, run_id: str, task: str, provider: str) -> dict[str, Any]:
    feature_payload = load_feature_list(paths.rpi_dir / "feature_list.json")
    sub_tasks = feature_payload["sub_tasks"]
    done_count, total_count = completion_counts(paths.rpi_dir / "feature_list.json")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "provider": provider,
        "mode": "instant",
        "task": task,
        "created_at": utc_now_iso(),
        "artifacts": {
            "research": str(paths.rpi_dir / "research.md"),
            "implementation": str(paths.rpi_dir / "implementation.md"),
            "plan": str(paths.rpi_dir / "plan.md"),
            "review": str(paths.review_file),
            "feature_list": str(paths.rpi_dir / "feature_list.json"),
            "tool_calls": str(paths.tool_calls_file),
        },
        "summary": {
            "sub_tasks_total": len(sub_tasks),
            "sub_tasks_done": done_count,
            "sub_tasks_pending": max(total_count - done_count, 0),
        },
        "guardrails": guardrail_summary(),
        "tool_catalog": tool_catalog_payload(paths),
        "instructions": [
            "instant mode selected: skip harness loop orchestration.",
            "coding agent may execute directly; call harness tools as needed.",
            "feature status updates must still use harness tool interfaces.",
        ],
    }


def _normalize_mode(mode: str) -> str:
    value = mode.strip().lower()
    if value not in VALID_MODES:
        raise ValueError(f"invalid mode '{mode}', expected one of: {sorted(VALID_MODES)}")
    return value


def _write_metrics(
    paths: RepoPaths,
    run_id: str,
    sessions: int,
    completed_override: bool | None = None,
    eval_report: dict[str, Any] | None = None,
    autocheck_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    done_count, total_count = completion_counts(paths.rpi_dir / "feature_list.json")
    usage_file = paths.runs_dir / run_id / "usage.json"
    usage_payload = read_json(usage_file, {"totals": {}})
    totals = usage_payload.get("totals", {})

    completed = done_count == total_count and total_count > 0
    if completed_override is not None:
        completed = bool(completed_override)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "executor_mode": "external_agent",
        "sessions": sessions,
        "sub_tasks_done": done_count,
        "sub_tasks_total": total_count,
        "completed": completed,
        "usage": {
            "input_tokens": int(totals.get("input_tokens", 0)),
            "output_tokens": int(totals.get("output_tokens", 0)),
            "total_tokens": int(totals.get("total_tokens", 0)),
            "estimated_cost_usd": float(totals.get("estimated_cost_usd", 0.0)),
            "actions": int(totals.get("actions", 0)),
            "verifier_checks": int(totals.get("verifier_checks", 0)),
        },
        "updated_at": utc_now_iso(),
    }

    if autocheck_report is not None:
        payload["autocheck"] = {
            "passed": bool(autocheck_report.get("passed", False)),
            "total_checks": int(autocheck_report.get("total_checks", 0)),
            "passed_checks": int(autocheck_report.get("passed_checks", 0)),
            "failed_checks": int(autocheck_report.get("failed_checks", 0)),
            "denied_checks": int(autocheck_report.get("denied_checks", 0)),
        }

    if eval_report is not None:
        payload["eval"] = {
            "passed": bool(eval_report.get("passed", False)),
            "profile": str(eval_report.get("profile", "default")),
            "report_file": str(paths.runs_dir / run_id / "evals" / "report.json"),
        }

    write_json(paths.runs_dir / run_id / "metrics.json", payload)
    return payload


def _run_harness_session(
    paths: RepoPaths,
    run_id: str,
    task: str,
    provider: str,
    run_autocheck_now: bool,
    autocheck_timeout_sec: int,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    run_dir = paths.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_tool_catalog(paths)

    meta = _load_session_meta(run_dir, provider)
    session_number = int(meta.get("session_count", 0)) + 1

    loop_context = _build_loop_context_payload(
        paths=paths,
        run_id=run_id,
        session_number=session_number,
        task=task,
        provider=provider,
    )
    write_json(run_dir / "loop_context.json", loop_context)

    _append_event(
        run_dir,
        {
            "type": "session_started",
            "run_id": run_id,
            "session_number": session_number,
            "provider": provider,
            "executor_mode": "external_agent",
            "mode": "planning",
            "task": task,
        },
    )
    _append_event(
        run_dir,
        {
            "type": "loop_context_written",
            "run_id": run_id,
            "session_number": session_number,
            "loop_context_file": str(run_dir / "loop_context.json"),
        },
    )

    autocheck_report: dict[str, Any] | None = None
    checks_run = 0
    if run_autocheck_now:
        autocheck_report = run_autocheck(
            paths=paths,
            run_id=run_id,
            sync_verifier_map=True,
            update_feature_status=True,
            timeout_sec=autocheck_timeout_sec,
        )
        checks_run = int(autocheck_report.get("total_checks", 0))
        _append_event(
            run_dir,
            {
                "type": "autocheck_post_session",
                "run_id": run_id,
                "session_number": session_number,
                "passed": bool(autocheck_report.get("passed", False)),
                "total_checks": checks_run,
                "passed_checks": int(autocheck_report.get("passed_checks", 0)),
                "failed_checks": int(autocheck_report.get("failed_checks", 0)),
            },
        )

    done_count, total_count = completion_counts(paths.rpi_dir / "feature_list.json")
    _append_event(
        run_dir,
        {
            "type": "session_finished",
            "run_id": run_id,
            "session_number": session_number,
            "done_count": done_count,
            "total_count": total_count,
            "autocheck_ran": run_autocheck_now,
        },
    )
    _append_progress(
        run_dir,
        (
            f"## Session {session_number}\n"
            f"- timestamp: {utc_now_iso()}\n"
            f"- executor_mode: external_agent\n"
            f"- task: {task}\n"
            f"- done/total: {done_count}/{total_count}\n"
            f"- autocheck_ran: {run_autocheck_now}\n\n"
        ),
    )

    meta["schema_version"] = SCHEMA_VERSION
    meta["provider"] = provider
    meta["executor_mode"] = "external_agent"
    meta["mode"] = "planning"
    meta["session_count"] = session_number
    meta["last_task"] = task
    meta["updated_at"] = utc_now_iso()
    write_json(run_dir / "session_meta.json", meta)

    _write_usage(paths=paths, run_id=run_id, session_number=session_number, checks_run=checks_run)
    return loop_context, autocheck_report, session_number


def run_task(
    paths: RepoPaths,
    task: str,
    provider: str = "codex",
    mode: str = "planning",
    run_id: str | None = None,
    context_threshold: float = 0.6,
    max_sessions: int = 30,
    runtime_approver: Any | None = None,
    structured_output: bool = True,
    eval_profile: str = "default",
    require_eval_pass: bool = True,
    eval_checks: list[EvalCheck] | None = None,
    run_autocheck_now: bool = True,
    autocheck_timeout_sec: int = 300,
) -> dict[str, Any]:
    del context_threshold, max_sessions, runtime_approver, structured_output

    ensure_repo_layout(paths)
    if not is_rpi_initialized(paths):
        init_rpi_artifacts(paths, task=task, provider_name=provider)

    mode_input = mode.strip().lower()
    if mode_input == "auto":
        raise ValueError("mode='auto' is not supported; choose planning or instant using the inline workflow.decide_mode policy")
    mode_input = _normalize_mode(mode_input)
    active_run_id = run_id or _next_run_id()
    run_dir = paths.runs_dir / active_run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    mode_decision = decide_mode(paths=paths, request=task, mode=mode_input, run_id=active_run_id)
    selected_mode = str(mode_decision.get("mode", "planning")).strip().lower()
    if selected_mode not in {"planning", "instant"}:
        raise ValueError(f"mode decision produced invalid runtime mode: {selected_mode}")
    touch_state(
        paths,
        last_run_id=active_run_id,
        provider=provider,
        executor_mode="external_agent",
        mode=selected_mode,
    )

    if selected_mode == "instant":
        meta = _load_session_meta(run_dir, provider)
        session_number = int(meta.get("session_count", 0)) + 1
        instant_context = _build_instant_context_payload(paths=paths, run_id=active_run_id, task=task, provider=provider)
        write_json(run_dir / "instant_context.json", instant_context)
        _append_event(
            run_dir,
            {
                "type": "session_started",
                "run_id": active_run_id,
                "session_number": session_number,
                "provider": provider,
                "executor_mode": "external_agent",
                "mode": "instant",
                "task": task,
            },
        )
        _append_event(
            run_dir,
            {
                "type": "instant_mode_selected",
                "run_id": active_run_id,
                "session_number": session_number,
            },
        )
        _append_event(
            run_dir,
            {
                "type": "instant_context_written",
                "run_id": active_run_id,
                "session_number": session_number,
                "instant_context_file": str(run_dir / "instant_context.json"),
            },
        )
        done_count, total_count = completion_counts(paths.rpi_dir / "feature_list.json")
        _append_event(
            run_dir,
            {
                "type": "session_finished",
                "run_id": active_run_id,
                "session_number": session_number,
                "done_count": done_count,
                "total_count": total_count,
                "autocheck_ran": False,
            },
        )
        _append_progress(
            run_dir,
            (
                f"## Session {session_number}\n"
                f"- timestamp: {utc_now_iso()}\n"
                f"- executor_mode: external_agent\n"
                f"- mode: instant\n"
                f"- task: {task}\n"
                f"- loop_skipped: true\n\n"
            ),
        )
        meta["schema_version"] = SCHEMA_VERSION
        meta["provider"] = provider
        meta["executor_mode"] = "external_agent"
        meta["mode"] = "instant"
        meta["session_count"] = session_number
        meta["last_task"] = task
        meta["updated_at"] = utc_now_iso()
        write_json(run_dir / "session_meta.json", meta)
        _write_usage(paths=paths, run_id=active_run_id, session_number=session_number, checks_run=0)
        loop_context = None
        autocheck_report = None
    else:
        loop_context, autocheck_report, session_number = _run_harness_session(
            paths=paths,
            run_id=active_run_id,
            task=task,
            provider=provider,
            run_autocheck_now=run_autocheck_now,
            autocheck_timeout_sec=autocheck_timeout_sec,
        )

    eval_report: dict[str, Any] | None = None
    completed_override: bool | None = None
    if all_completed(paths.rpi_dir / "feature_list.json"):
        eval_report = run_eval_suite(
            paths=paths,
            run_id=active_run_id,
            profile=eval_profile,
            extra_checks=eval_checks,
        )
        _append_event(
            run_dir,
            {
                "type": "eval_completed",
                "run_id": active_run_id,
                "profile": eval_profile,
                "passed": bool(eval_report.get("passed", False)),
            },
        )
        if require_eval_pass and not bool(eval_report.get("passed", False)):
            completed_override = False
            _append_event(
                run_dir,
                {
                    "type": "eval_gate_blocked_completion",
                    "run_id": active_run_id,
                    "profile": eval_profile,
                },
            )

    metrics = _write_metrics(
        paths=paths,
        run_id=active_run_id,
        sessions=session_number,
        completed_override=completed_override,
        eval_report=eval_report,
        autocheck_report=autocheck_report,
    )
    provider_session = write_provider_session(
        paths=paths,
        run_id=active_run_id,
        provider=provider,
        task=task,
        mode=selected_mode,
    )
    return {
        "run_id": active_run_id,
        "provider": provider,
        "executor_mode": "external_agent",
        "mode": selected_mode,
        "mode_decision": mode_decision,
        "metrics": metrics,
        "loop_context_file": str(run_dir / "loop_context.json") if selected_mode == "planning" else None,
        "loop_context": loop_context,
        "instant_context_file": str(run_dir / "instant_context.json") if selected_mode == "instant" else None,
        "instant_context": instant_context if selected_mode == "instant" else None,
        "autocheck": autocheck_report,
        "eval": eval_report,
        "provider_session_file": provider_session["session_file"],
        "provider_session": provider_session,
    }


def resume_task(
    paths: RepoPaths,
    task: str = "resume",
    provider: str = "codex",
    mode: str | None = None,
    run_id: str | None = None,
    context_threshold: float = 0.6,
    max_sessions: int = 30,
    runtime_approver: Any | None = None,
    structured_output: bool = True,
    eval_profile: str = "default",
    require_eval_pass: bool = True,
    eval_checks: list[EvalCheck] | None = None,
    run_autocheck_now: bool = True,
    autocheck_timeout_sec: int = 300,
) -> dict[str, Any]:
    state = read_json(paths.state_file, {"last_run_id": None, "provider": provider, "mode": "planning"})
    active_run = run_id or state.get("last_run_id")
    if not active_run:
        raise ValueError("no previous run found")
    effective_provider = provider or str(state.get("provider", "codex"))
    effective_mode = _normalize_mode(mode or str(state.get("mode", "planning")))
    return run_task(
        paths=paths,
        task=task,
        provider=effective_provider,
        mode=effective_mode,
        run_id=active_run,
        context_threshold=context_threshold,
        max_sessions=max_sessions,
        runtime_approver=runtime_approver,
        structured_output=structured_output,
        eval_profile=eval_profile,
        require_eval_pass=require_eval_pass,
        eval_checks=eval_checks,
        run_autocheck_now=run_autocheck_now,
        autocheck_timeout_sec=autocheck_timeout_sec,
    )


def status(paths: RepoPaths, run_id: str | None = None) -> dict[str, Any]:
    state = read_json(paths.state_file, {"last_run_id": None, "provider": "codex", "mode": "planning"})
    active_run = run_id or state.get("last_run_id")
    done_count, total_count = completion_counts(paths.rpi_dir / "feature_list.json")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": state.get("contract_version", "1.0"),
        "run_id": active_run,
        "provider": state.get("provider", "codex"),
        "executor_mode": state.get("executor_mode", "external_agent"),
        "mode": state.get("mode", "planning"),
        "done_count": done_count,
        "total_count": total_count,
        "completed": done_count == total_count and total_count > 0,
    }
    if active_run:
        run_dir = paths.runs_dir / active_run
        metrics_file = run_dir / "metrics.json"
        if metrics_file.exists():
            payload["metrics"] = read_json(metrics_file, {})
        usage_file = run_dir / "usage.json"
        if usage_file.exists():
            payload["usage"] = read_json(usage_file, {}).get("totals", {})
        loop_context_file = run_dir / "loop_context.json"
        if loop_context_file.exists():
            payload["loop_context_file"] = str(loop_context_file)
        instant_context_file = run_dir / "instant_context.json"
        if instant_context_file.exists():
            payload["instant_context_file"] = str(instant_context_file)
        autocheck_file = run_dir / "autocheck" / "report.json"
        if autocheck_file.exists():
            report = read_json(autocheck_file, {})
            payload["autocheck"] = {
                "passed": bool(report.get("passed", False)),
                "total_checks": int(report.get("total_checks", 0)),
                "passed_checks": int(report.get("passed_checks", 0)),
                "failed_checks": int(report.get("failed_checks", 0)),
            }
    return payload


def intervene(paths: RepoPaths, reason: str, run_id: str | None = None) -> dict[str, Any]:
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id") or _next_run_id()
    run_dir = paths.runs_dir / active_run
    run_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": active_run,
        "reason": reason,
        "latest_snapshot": None,
        "created_at": utc_now_iso(),
        "status": "requested",
    }
    snapshots_dir = run_dir / "snapshots"
    if snapshots_dir.exists():
        latest = sorted(snapshots_dir.glob("session_*.json"))
        if latest:
            payload["latest_snapshot"] = str(latest[-1])

    write_json(run_dir / "intervention.json", payload)
    _append_event(
        run_dir,
        {
            "type": "intervention_requested",
            "run_id": active_run,
            "reason": reason,
            "executor_mode": "external_agent",
        },
    )
    return payload


def fork_run(paths: RepoPaths, source_run_id: str, target_run_id: str | None = None) -> dict[str, Any]:
    source_dir = paths.runs_dir / source_run_id
    if not source_dir.exists():
        raise ValueError(f"source run does not exist: {source_run_id}")

    active_target = target_run_id or f"{source_run_id}_fork_{utc_now_iso().replace(':', '')}"
    target_dir = paths.runs_dir / active_target
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in [
        "events.jsonl",
        "progress.md",
        "session_meta.json",
        "usage.json",
        "metrics.json",
        "loop_context.json",
        "instant_context.json",
    ]:
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, target_dir / name)
    for dirname in ["snapshots", "checkpoints", "autocheck", "evals"]:
        src = source_dir / dirname
        dst = target_dir / dirname
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_run_id": source_run_id,
        "target_run_id": active_target,
        "created_at": utc_now_iso(),
    }
    write_json(target_dir / "fork.json", payload)
    _append_event(
        target_dir,
        {
            "type": "run_forked",
            "source_run_id": source_run_id,
            "target_run_id": active_target,
            "executor_mode": "external_agent",
        },
    )
    touch_state(paths, last_run_id=active_target, executor_mode="external_agent")
    return payload
