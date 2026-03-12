import ast
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from pydantic import BaseModel, Field, ValidationError
import yaml

from .config import RepoPaths, SCHEMA_VERSION, ensure_repo_layout, read_json, utc_now_iso, write_json
from .policy import PolicyEngine
from .security import validate_pytest_target, validate_repo_relative_path, validate_timeout
from .tracker import assert_status_only_mutation, load_feature_list

VERIFIER_YAML_TEMPLATE = f"""# Autoeval verifier configuration
# Fixed template: developer/end-user maintains this file.
# Coding agent should not author verifier entries; it only consumes resolved outputs.
schema_version: {SCHEMA_VERSION}
tests:
  # Link a single test file
  # - path: tests/unit/test_example.py
  #   scope: file
  #   framework: pytest
  #   pattern: "test_*.py"
  #   recursive: false
  #   mcp_profiles: []

  # Link an entire test directory
  # - path: tests/unit
  #   scope: directory
  #   framework: pytest
  #   pattern: "test_*.py"
  #   recursive: true
  #   mcp_profiles: []

prompts:
  # Reserved standardized section for future MCP/service-backed verifier inputs.
  # Phase 1 keeps this declarative only; runtime execution is deferred.
  # - id: linear_triage
  #   profile: linear
  #   prompt: "List backend issues marked blocked"
  #   required: false

connections:
  # Reserved standardized section for future connector/MCP dependencies.
  # Phase 1 keeps this declarative only; runtime execution is deferred.
  # - id: jira
  #   profile: jira
  #   purpose: issue_tracking
  #   required: false
"""

class VerifierTest(BaseModel):
    path: str
    scope: str = "file"  # file | directory
    framework: str = "pytest"
    pattern: str = "test_*.py"
    recursive: bool = True
    mcp_profiles: list[str] = Field(default_factory=list)


class VerifierConfig(BaseModel):
    schema_version: int = SCHEMA_VERSION
    tests: list[VerifierTest] = Field(default_factory=list)
    prompts: list[dict[str, Any]] = Field(default_factory=list)
    connections: list[dict[str, Any]] = Field(default_factory=list)


class AutocheckTarget(BaseModel):
    target_id: str
    target: str
    kind: str
    path: str
    node_id: str = ""
    framework: str = "pytest"
    mcp_profiles: list[str] = Field(default_factory=list)
    link_ids: list[str] = Field(default_factory=list)


class AutocheckMap(BaseModel):
    schema_version: int = SCHEMA_VERSION
    generated_at: str | None = None
    links: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[AutocheckTarget] = Field(default_factory=list)


def default_verifier_payload() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tests": [], "prompts": [], "connections": []}


def verifier_template_text() -> str:
    return VERIFIER_YAML_TEMPLATE


def ensure_verifier_file(paths: RepoPaths) -> None:
    if paths.verifier_file.exists():
        return
    paths.verifier_file.parent.mkdir(parents=True, exist_ok=True)
    paths.verifier_file.write_text(verifier_template_text(), encoding="utf-8")


def load_verifier_config(paths: RepoPaths) -> VerifierConfig:
    ensure_verifier_file(paths)
    raw_text = paths.verifier_file.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid verifier yaml: {exc}") from exc
    if raw is None:
        return VerifierConfig()
    if not isinstance(raw, dict):
        raise ValueError("invalid verifier yaml: top-level document must be a mapping")

    normalized_tests: list[dict[str, Any]] = []
    raw_tests = raw.get("tests", [])
    if raw_tests is None:
        raw_tests = []
    elif not isinstance(raw_tests, list):
        raise ValueError("invalid verifier yaml: 'tests' must be a list")
    for index, item in enumerate(raw_tests, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"invalid verifier yaml: test entry {index} must be a mapping")
        path_value = str(item.get("path", "")).strip()
        if not path_value:
            raise ValueError(f"invalid verifier yaml: test entry {index} must define a non-empty path")
        mcp_profiles = item.get("mcp_profiles", [])
        if not isinstance(mcp_profiles, list):
            raise ValueError(f"invalid verifier yaml: test entry {index} field 'mcp_profiles' must be a list")
        normalized_tests.append(
            {
                "path": path_value,
                "scope": str(item.get("scope", "file") or "file"),
                "framework": str(item.get("framework", "pytest") or "pytest"),
                "pattern": str(item.get("pattern", "test_*.py") or "test_*.py"),
                "recursive": bool(item.get("recursive", True)),
                "mcp_profiles": [str(value) for value in mcp_profiles if str(value).strip()],
            }
        )

    normalized_prompts = _normalize_named_verifier_section(
        raw.get("prompts", []),
        field_name="prompts",
        required_fields=("id", "profile", "prompt"),
    )
    normalized_connections = _normalize_named_verifier_section(
        raw.get("connections", []),
        field_name="connections",
        required_fields=("id", "profile"),
        optional_fields=("purpose",),
    )

    payload = {
        "schema_version": int(raw.get("schema_version", SCHEMA_VERSION)),
        "tests": normalized_tests,
        "prompts": normalized_prompts,
        "connections": normalized_connections,
    }
    try:
        return VerifierConfig.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid verifier config: {exc}") from exc


def _normalize_named_verifier_section(
    raw_section: Any,
    *,
    field_name: str,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    if raw_section is None:
        return []
    if not isinstance(raw_section, list):
        raise ValueError(f"invalid verifier yaml: '{field_name}' must be a list")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_section, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"invalid verifier yaml: {field_name} entry {index} must be a mapping")

        entry: dict[str, Any] = {"required": bool(item.get("required", True))}
        for key in required_fields:
            value = str(item.get(key, "")).strip()
            if not value:
                raise ValueError(f"invalid verifier yaml: {field_name} entry {index} must define '{key}'")
            entry[key] = value
        for key in optional_fields:
            value = str(item.get(key, "")).strip()
            if value:
                entry[key] = value
        normalized.append(entry)

    return normalized


def _normalize_rel_path(path_value: str) -> str:
    normalized = path_value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _collect_pytest_nodes(file_path: Path) -> list[str]:
    if not file_path.exists() or not file_path.is_file():
        return []

    source = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    nodes: list[str] = []
    for item in tree.body:
        if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
            nodes.append(item.name)
        elif isinstance(item, ast.ClassDef) and item.name.startswith("Test"):
            for child in item.body:
                if isinstance(child, ast.FunctionDef) and child.name.startswith("test_"):
                    nodes.append(f"{item.name}::{child.name}")
    return nodes


def _target_id(target: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", target).strip("_").lower()
    return f"target_{safe}"


def _iter_linked_files(paths: RepoPaths, test_item: VerifierTest) -> tuple[str, list[str]]:
    rel_path = _normalize_rel_path(test_item.path)
    path_validation = validate_repo_relative_path(rel_path)
    if not path_validation.allowed:
        raise ValueError(f"invalid verifier path '{test_item.path}': {path_validation.reason}")

    framework = test_item.framework.strip().lower()
    if framework != "pytest":
        raise ValueError(f"unsupported verifier framework '{test_item.framework}': only pytest is supported")

    scope = test_item.scope.strip().lower() or "file"
    absolute = paths.repo / rel_path
    if scope not in {"file", "directory"}:
        raise ValueError(f"invalid verifier scope '{test_item.scope}' for path '{test_item.path}'")

    if scope == "file":
        if not absolute.exists() or not absolute.is_file():
            raise ValueError(f"linked verifier file does not exist: {rel_path}")
        return rel_path, [rel_path]

    if not absolute.exists() or not absolute.is_dir():
        raise ValueError(f"linked verifier directory does not exist: {rel_path}")

    pattern = test_item.pattern.strip() or "test_*.py"
    iterator = absolute.rglob(pattern) if bool(test_item.recursive) else absolute.glob(pattern)
    files = sorted(
        {
            str(path.relative_to(paths.repo).as_posix())
            for path in iterator
            if path.is_file() and path.suffix == ".py"
        }
    )
    return rel_path, files


def _upsert_target(
    target_index: dict[str, dict[str, Any]],
    *,
    target: str,
    kind: str,
    path: str,
    node_id: str,
    link_id: str,
    framework: str,
    mcp_profiles: list[str],
) -> None:
    entry = target_index.get(target)
    if entry is None:
        entry = {
            "target_id": _target_id(target),
            "target": target,
            "kind": kind,
            "path": path,
            "node_id": node_id,
            "framework": framework,
            "mcp_profiles": sorted(set(mcp_profiles)),
            "link_ids": [link_id],
        }
        target_index[target] = entry
        return

    entry["mcp_profiles"] = sorted(set(entry.get("mcp_profiles", []) + list(mcp_profiles)))
    entry["link_ids"] = sorted(set(entry.get("link_ids", []) + [link_id]))


def build_autocheck_map_from_verifier(paths: RepoPaths) -> dict[str, Any]:
    config = load_verifier_config(paths)

    links: list[dict[str, Any]] = []
    target_index: dict[str, dict[str, Any]] = {}

    for index, test_item in enumerate(config.tests, start=1):
        link_id = f"link_{index}"
        root_rel_path, linked_files = _iter_linked_files(paths, test_item)
        scope = test_item.scope.strip().lower() or "file"
        framework = test_item.framework.strip().lower()
        mcp_profiles = sorted(set(test_item.mcp_profiles))

        links.append(
            {
                "link_id": link_id,
                "path": root_rel_path,
                "scope": scope,
                "framework": framework,
                "pattern": test_item.pattern,
                "recursive": bool(test_item.recursive),
                "resolved_files": linked_files,
                "mcp_profiles": mcp_profiles,
            }
        )

        if scope == "directory":
            dir_target_validation = validate_pytest_target(root_rel_path)
            if not dir_target_validation.allowed:
                raise ValueError(
                    f"invalid directory pytest target '{root_rel_path}': {dir_target_validation.reason}"
                )
            _upsert_target(
                target_index,
                target=root_rel_path,
                kind="directory",
                path=root_rel_path,
                node_id="",
                link_id=link_id,
                framework=framework,
                mcp_profiles=mcp_profiles,
            )

        for file_rel in linked_files:
            file_target_validation = validate_pytest_target(file_rel)
            if not file_target_validation.allowed:
                raise ValueError(f"invalid file pytest target '{file_rel}': {file_target_validation.reason}")

            _upsert_target(
                target_index,
                target=file_rel,
                kind="file",
                path=file_rel,
                node_id="",
                link_id=link_id,
                framework=framework,
                mcp_profiles=mcp_profiles,
            )

            file_nodes = _collect_pytest_nodes(paths.repo / file_rel)
            for node_id in file_nodes:
                node_target = f"{file_rel}::{node_id}"
                node_target_validation = validate_pytest_target(node_target)
                if not node_target_validation.allowed:
                    raise ValueError(f"invalid node pytest target '{node_target}': {node_target_validation.reason}")
                _upsert_target(
                    target_index,
                    target=node_target,
                    kind="node",
                    path=file_rel,
                    node_id=node_id,
                    link_id=link_id,
                    framework=framework,
                    mcp_profiles=mcp_profiles,
                )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "links": links,
        "targets": sorted(target_index.values(), key=lambda item: str(item.get("target", ""))),
    }
    return payload


def sync_autocheck_map_from_verifier(paths: RepoPaths) -> dict[str, Any]:
    ensure_repo_layout(paths)
    map_payload = build_autocheck_map_from_verifier(paths)
    return {
        "ok": True,
        "link_count": len(map_payload.get("links", [])),
        "target_count": len(map_payload.get("targets", [])),
        "links": map_payload.get("links", []),
        "targets": map_payload.get("targets", []),
    }


def sync_feature_list_from_verifier(paths: RepoPaths) -> dict[str, Any]:
    # Backward-compatible alias. It now syncs only autocheck_map from verifier links.
    return sync_autocheck_map_from_verifier(paths)


def mapped_target_ids(paths: RepoPaths) -> set[str]:
    return {item.target for item in _build_autocheck_map(paths).targets}


def mapped_sub_task_ids(paths: RepoPaths) -> set[str]:
    # Backward-compatible alias retained for existing imports.
    return mapped_target_ids(paths)


def _append_event(paths: RepoPaths, run_id: str, payload: dict[str, Any]) -> None:
    event_file = paths.runs_dir / run_id / "events.jsonl"
    event_file.parent.mkdir(parents=True, exist_ok=True)
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": utc_now_iso(), **payload}, sort_keys=True))
        handle.write("\n")


def _run_pytest_target(repo: Path, target: str, timeout_sec: int) -> dict[str, Any]:
    pytest_bin = os.getenv("AUTOEVAL_PYTEST_BIN", "pytest")
    command = [pytest_bin, "-q", target]
    try:
        result = subprocess.run(
            command,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return {
            "status": "failed",
            "exit_code": 127,
            "stdout": "",
            "stderr": f"pytest binary not found: {pytest_bin}",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "exit_code": None,
            "stdout": "",
            "stderr": "pytest target timed out",
        }

    return {
        "status": "completed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "stdout": (result.stdout or "")[-4000:],
        "stderr": (result.stderr or "")[-4000:],
    }


def _task_targets_from_verifications(task: dict[str, Any], linked_targets: set[str]) -> tuple[set[str], set[str]]:
    referenced: set[str] = set()
    unresolved: set[str] = set()

    verifications = task["verifications"]
    if not verifications:
        unresolved.add("missing_verifications")
        return referenced, unresolved
    for item in verifications:
        kind = str(item.get("kind", "")).strip().lower()
        target = str(item.get("target", "")).strip()
        if not target:
            continue

        if kind != "pytest":
            unresolved.add(f"{kind}:{target}")
            continue

        target_check = validate_pytest_target(target)
        if not target_check.allowed:
            unresolved.add(target)
            continue

        if target in linked_targets:
            referenced.add(target)
        else:
            unresolved.add(target)

    return referenced, unresolved


def _load_feature_payload(paths: RepoPaths) -> dict[str, Any]:
    return load_feature_list(paths.rpi_dir / "feature_list.json")


def _build_autocheck_map(paths: RepoPaths) -> AutocheckMap:
    payload = build_autocheck_map_from_verifier(paths)
    try:
        return AutocheckMap.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid verifier target map: {exc}") from exc


def _select_autocheck_targets(
    *,
    feature_payload: dict[str, Any],
    linked_targets: set[str],
    selection_mode: str,
    requested_targets: list[str],
) -> tuple[list[str], list[str], dict[str, list[str]], dict[str, list[str]]]:
    mode = selection_mode.strip().lower()
    if mode not in {"feature-list", "all"}:
        raise ValueError("selection_mode must be 'feature-list' or 'all'")

    unknown_requested = [target for target in requested_targets if target not in linked_targets]
    if requested_targets:
        selected = sorted({target for target in requested_targets if target in linked_targets})
        return selected, unknown_requested, {}, {}

    if mode == "all":
        return sorted(linked_targets), [], {}, {}

    task_ref_map: dict[str, list[str]] = {}
    task_unlinked_map: dict[str, list[str]] = {}
    selected_targets: set[str] = set()

    for task in feature_payload["sub_tasks"]:
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            continue
        task_targets, unlinked = _task_targets_from_verifications(task, linked_targets)
        if task_targets:
            task_ref_map[task_id] = sorted(task_targets)
            selected_targets.update(task_targets)
        if unlinked:
            task_unlinked_map[task_id] = sorted(unlinked)

    return sorted(selected_targets), [], task_ref_map, task_unlinked_map


def run_autocheck(
    paths: RepoPaths,
    run_id: str,
    sync_verifier_map: bool = True,
    update_feature_status: bool = True,
    timeout_sec: int = 300,
    selection_mode: str = "feature-list",
    targets: list[str] | None = None,
) -> dict[str, Any]:
    ensure_repo_layout(paths)
    timeout_validation = validate_timeout(timeout_sec)
    if not timeout_validation.allowed:
        raise ValueError(timeout_validation.reason)

    if sync_verifier_map:
        sync_autocheck_map_from_verifier(paths)

    autocheck_map = _build_autocheck_map(paths)
    target_index = {item.target: item for item in autocheck_map.targets}
    linked_targets = set(target_index.keys())

    feature_payload = _load_feature_payload(paths)
    requested_targets = [str(value).strip() for value in (targets or []) if str(value).strip()]
    selected_targets, unknown_requested_targets, task_ref_map, task_unlinked_map = _select_autocheck_targets(
        feature_payload=feature_payload,
        linked_targets=linked_targets,
        selection_mode=selection_mode,
        requested_targets=requested_targets,
    )

    from .connectors import resolve_runtime_profiles

    runtime_profiles = resolve_runtime_profiles(paths)
    available_mcp = {name for name in runtime_profiles.keys()} | {
        str(profile.get("tool_namespace", "")) for profile in runtime_profiles.values() if profile.get("tool_namespace")
    }

    policy_engine = PolicyEngine(no_network=True)
    pytest_bin = os.getenv("AUTOEVAL_PYTEST_BIN", "pytest")

    results: list[dict[str, Any]] = []
    denied_targets: list[str] = []
    passed_targets: list[str] = []
    failed_targets: list[str] = []

    for target in selected_targets:
        target_meta = target_index[target]
        target_id = target_meta.target_id
        mcp_profiles = target_meta.mcp_profiles

        policy_decision = policy_engine.evaluate_terminal_command(
            command=f"{pytest_bin} -q {target}",
            target=target,
            metadata={"source": "autocheck", "target_id": target_id, "no_network": True},
        )
        missing_mcp = [name for name in mcp_profiles if name not in available_mcp]

        if not policy_decision.allowed:
            execution = {
                "status": "blocked",
                "exit_code": None,
                "stdout": "",
                "stderr": policy_decision.reason,
            }
            denied_targets.append(target)
            _append_event(
                paths,
                run_id,
                {
                    "type": "autocheck_guardrail_denied",
                    "run_id": run_id,
                    "target": target,
                    "target_id": target_id,
                    "reason": policy_decision.reason,
                    "policy_stage": policy_decision.policy_stage,
                },
            )
        else:
            execution = _run_pytest_target(paths.repo, target=target, timeout_sec=timeout_sec)

        passed = execution["status"] == "completed" and not missing_mcp
        if passed:
            passed_targets.append(target)
        else:
            failed_targets.append(target)

        results.append(
            {
                "target": target,
                "target_id": target_id,
                "kind": target_meta.kind,
                "path": target_meta.path,
                "node_id": target_meta.node_id,
                "passed": passed,
                "policy": policy_decision.model_dump(),
                "missing_mcp_profiles": missing_mcp,
                "execution": execution,
            }
        )

    updated_task_ids: list[str] = []
    blocked_task_ids: list[str] = []
    if update_feature_status:
        before_feature = _load_feature_payload(paths)
        feature_payload = _load_feature_payload(paths)
        result_by_target = {item["target"]: item for item in results}

        for task in feature_payload["sub_tasks"]:
            task_id = str(task.get("id", "")).strip()
            if not task_id:
                continue

            task_targets, unlinked_refs = _task_targets_from_verifications(task, linked_targets)
            if unlinked_refs:
                task["status"] = False
                blocked_task_ids.append(task_id)
                updated_task_ids.append(task_id)
                continue

            if not task_targets:
                continue

            if not task_targets.issubset(set(selected_targets)):
                # Partial target run: do not mutate unrelated task state.
                continue

            task_pass = all(bool(result_by_target.get(target, {}).get("passed", False)) for target in task_targets)
            task["status"] = task_pass
            updated_task_ids.append(task_id)

        assert_status_only_mutation(before_feature, feature_payload)
        write_json(paths.rpi_dir / "feature_list.json", feature_payload)

    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "selection_mode": selection_mode,
        "requested_targets": requested_targets,
        "unknown_requested_targets": sorted(set(unknown_requested_targets)),
        "linked_target_count": len(linked_targets),
        "selected_targets": selected_targets,
        "selected_target_count": len(selected_targets),
        "passed": bool(results) and all(item["passed"] for item in results),
        "total_checks": len(results),
        "passed_checks": len(passed_targets),
        "failed_checks": len(failed_targets),
        "denied_checks": len(denied_targets),
        "passed_targets": sorted(set(passed_targets)),
        "failed_targets": sorted(set(failed_targets)),
        "denied_targets": sorted(set(denied_targets)),
        "feature_task_target_refs": task_ref_map,
        "feature_task_unlinked_refs": task_unlinked_map,
        "updated_feature_status": update_feature_status,
        "updated_task_ids": sorted(set(updated_task_ids)),
        "blocked_task_ids": sorted(set(blocked_task_ids)),
        "results": results,
    }

    report_file = paths.runs_dir / run_id / "autocheck" / "report.json"
    write_json(report_file, report)
    _append_event(
        paths,
        run_id,
        {
            "type": "autocheck_completed",
            "run_id": run_id,
            "passed": report["passed"],
            "total_checks": report["total_checks"],
            "passed_checks": report["passed_checks"],
            "failed_checks": report["failed_checks"],
            "denied_checks": report["denied_checks"],
            "report_file": str(report_file),
        },
    )
    return report
