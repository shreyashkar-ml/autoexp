import json
from pathlib import Path
from typing import Any, Callable

from .config import RepoPaths, SCHEMA_VERSION, read_json, utc_now_iso, write_json
from .tracker import completion_counts, load_feature_list
from .verifier import build_autocheck_map_from_verifier

EvalCheck = Callable[[RepoPaths, str], dict[str, Any]]


def _run_dir(paths: RepoPaths, run_id: str) -> Path:
    run_dir = paths.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _load_events(events_file: Path) -> list[dict[str, Any]]:
    if not events_file.exists():
        return []
    events: list[dict[str, Any]] = []
    for index, raw_line in enumerate(events_file.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid event json on line {index}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"invalid event payload on line {index}: expected object")
        events.append(parsed)
    return events


def _check_required_artifacts(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(paths, run_id)
    required_core = [
        run_dir / "events.jsonl",
        run_dir / "progress.md",
        run_dir / "session_meta.json",
        run_dir / "usage.json",
    ]
    missing_core = [str(path) for path in required_core if not path.exists()]
    has_context = (run_dir / "loop_context.json").exists() or (run_dir / "instant_context.json").exists()
    missing = list(missing_core)
    if not has_context:
        missing.append("loop_context.json or instant_context.json")
    return {
        "id": "required_artifacts",
        "passed": not missing,
        "severity": "error",
        "summary": "Required run artifacts exist",
        "evidence": {"missing": missing},
    }


def _check_instruction_artifact_consistency(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    del run_id
    required = [
        paths.rpi_dir / "research.md",
        paths.rpi_dir / "implementation.md",
        paths.rpi_dir / "plan.md",
        paths.review_file,
        paths.rpi_dir / "feature_list.json",
        paths.verifier_file,
        paths.tool_calls_file,
    ]
    missing = [str(path) for path in required if not path.exists()]
    return {
        "id": "instruction_artifacts_consistent",
        "passed": not missing,
        "severity": "error",
        "summary": "Instruction and runtime contract artifacts exist in maintained locations",
        "evidence": {"missing": missing},
    }


def _check_session_lifecycle(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    events = _load_events(_run_dir(paths, run_id) / "events.jsonl")
    counts = {
        "session_started": 0,
        "session_finished": 0,
        "loop_context_written": 0,
        "instant_mode_selected": 0,
        "instant_context_written": 0,
    }
    for event in events:
        event_type = str(event.get("type", ""))
        if event_type in counts:
            counts[event_type] += 1
    started = counts["session_started"]
    finished = counts["session_finished"]
    loop_context = counts["loop_context_written"]
    instant_mode = counts["instant_mode_selected"]
    instant_context = counts["instant_context_written"]
    planning_ok = loop_context >= started and instant_mode == 0
    instant_ok = instant_mode >= started and instant_context >= started
    passed = started > 0 and started == finished and (planning_ok or instant_ok)
    return {
        "id": "session_lifecycle",
        "passed": passed,
        "severity": "error",
        "summary": "Harness session lifecycle and loop-context accounting are consistent",
        "evidence": counts,
    }


def _check_feature_completion(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    done_count, total_count = completion_counts(paths.rpi_dir / "feature_list.json")
    return {
        "id": "feature_completion",
        "passed": total_count > 0 and done_count == total_count,
        "severity": "error",
        "summary": "Feature list is fully completed",
        "evidence": {"done_count": done_count, "total_count": total_count},
    }


def _check_feature_list_invariants(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    del run_id
    payload = load_feature_list(paths.rpi_dir / "feature_list.json")
    return {
        "id": "feature_list_invariants",
        "passed": True,
        "severity": "error",
        "summary": "Feature list satisfies normalized invariants",
        "evidence": {"sub_task_count": len(payload["sub_tasks"])},
    }


def _check_verifier_target_consistency(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    del run_id
    verifier_map = build_autocheck_map_from_verifier(paths)
    linked_targets = {str(item.get("target", "")).strip() for item in verifier_map.get("targets", [])}
    feature_payload = load_feature_list(paths.rpi_dir / "feature_list.json")

    unresolved: dict[str, list[str]] = {}
    for task in feature_payload["sub_tasks"]:
        task_id = str(task.get("id", "")).strip()
        missing_targets = [
            str(entry.get("target", "")).strip()
            for entry in task["verifications"]
            if str(entry.get("kind", "")).strip().lower() == "pytest"
            and str(entry.get("target", "")).strip() not in linked_targets
        ]
        if missing_targets:
            unresolved[task_id] = missing_targets

    return {
        "id": "verifier_target_consistency",
        "passed": not unresolved,
        "severity": "error",
        "summary": "Feature verifications reference resolved verifier targets",
        "evidence": {
            "linked_target_count": len(linked_targets),
            "unresolved": unresolved,
        },
    }


def _check_autocheck_pass(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    report_file = _run_dir(paths, run_id) / "autocheck" / "report.json"
    if not report_file.exists():
        return {
            "id": "autocheck_passed",
            "passed": False,
            "severity": "error",
            "summary": "Autocheck report exists and passes",
            "evidence": {"missing_report": str(report_file)},
        }

    report = read_json(report_file, {})
    return {
        "id": "autocheck_passed",
        "passed": bool(report.get("passed", False)),
        "severity": "error",
        "summary": "Autocheck checks pass for selected linked verifier targets",
        "evidence": {
            "passed_checks": int(report.get("passed_checks", 0)),
            "failed_checks": int(report.get("failed_checks", 0)),
        },
    }


def _check_guardrail_denials(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    events = _load_events(_run_dir(paths, run_id) / "events.jsonl")
    denials = 0
    for event in events:
        if event.get("type") != "autocheck_guardrail_denied":
            continue
        denials += 1
    return {
        "id": "no_guardrail_denials",
        "passed": denials == 0,
        "severity": "error",
        "summary": "No guardrail denials were recorded during harness verification",
        "evidence": {"denials": denials},
    }


def _check_run_state_consistency(paths: RepoPaths, run_id: str) -> dict[str, Any]:
    state = read_json(paths.state_file, {"last_run_id": None})
    run_dir = _run_dir(paths, run_id)
    metrics = read_json(run_dir / "metrics.json", {})
    usage = read_json(run_dir / "usage.json", {"sessions": [], "totals": {}})
    session_meta = read_json(run_dir / "session_meta.json", {"session_count": 0})

    mismatches: list[str] = []
    if metrics and str(metrics.get("run_id", "")) != run_id:
        mismatches.append("metrics.run_id")
    if usage and str(usage.get("run_id", run_id)) != run_id:
        mismatches.append("usage.run_id")
    if metrics and int(metrics.get("sessions", 0)) != int(session_meta.get("session_count", 0)):
        mismatches.append("metrics.sessions")
    if state.get("last_run_id") not in {None, run_id}:
        mismatches.append("state.last_run_id")

    return {
        "id": "run_state_consistency",
        "passed": not mismatches,
        "severity": "error",
        "summary": "Run state, session metadata, and metrics are internally consistent",
        "evidence": {"mismatches": mismatches},
    }


def _normalize_check(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(result.get("id", "unknown_check")),
        "passed": bool(result.get("passed", False)),
        "severity": str(result.get("severity", "error")),
        "summary": str(result.get("summary", "")),
        "evidence": dict(result.get("evidence", {})),
    }


def default_eval_checks() -> list[EvalCheck]:
    return [
        _check_required_artifacts,
        _check_instruction_artifact_consistency,
        _check_session_lifecycle,
        _check_run_state_consistency,
        _check_feature_list_invariants,
        _check_verifier_target_consistency,
        _check_feature_completion,
        _check_autocheck_pass,
        _check_guardrail_denials,
    ]


def run_eval_suite(
    paths: RepoPaths,
    run_id: str,
    profile: str = "default",
    extra_checks: list[EvalCheck] | None = None,
) -> dict[str, Any]:
    _run_dir(paths, run_id)
    checks = default_eval_checks() + list(extra_checks or [])

    results = [_normalize_check(check(paths, run_id)) for check in checks]

    passed = all(item["passed"] for item in results)
    failures = [item["id"] for item in results if not item["passed"]]
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "profile": profile,
        "created_at": utc_now_iso(),
        "passed": passed,
        "checks": results,
        "summary": {
            "total_checks": len(results),
            "passed_checks": sum(1 for item in results if item["passed"]),
            "failed_checks": failures,
        },
    }
    write_json(paths.runs_dir / run_id / "evals" / "report.json", report)
    return report


def load_latest_eval_report(paths: RepoPaths, run_id: str) -> dict[str, Any] | None:
    report_file = paths.runs_dir / run_id / "evals" / "report.json"
    if not report_file.exists():
        return None
    return read_json(report_file, {})
