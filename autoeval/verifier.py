from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

from pydantic import BaseModel, Field

from .config import RepoPaths, SCHEMA_VERSION, ensure_repo_layout, read_json, utc_now_iso, write_json
from .policy import PolicyEngine
from .security import validate_pytest_target, validate_repo_relative_path, validate_timeout
from .tracker import assert_status_only_mutation

VERIFIER_YAML_TEMPLATE = f"""# Autoeval verifier configuration
# Fixed template: developer/end-user maintains this file.
# Coding agent should not author links; it only consumes linked targets.
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
"""

PYTEST_OPTIONS_WITH_VALUE = {
    "-k",
    "-m",
    "--maxfail",
    "--tb",
    "--rootdir",
    "-c",
    "--confcutdir",
    "--durations",
    "--junitxml",
    "--cov",
    "--cov-report",
}


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


def _new_test_entry() -> dict[str, Any]:
    return {
        "path": "",
        "scope": "file",
        "framework": "pytest",
        "pattern": "test_*.py",
        "recursive": True,
        "mcp_profiles": [],
    }


def default_verifier_payload() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tests": []}


def verifier_template_text() -> str:
    return VERIFIER_YAML_TEMPLATE


def _parse_inline_list(value: str) -> list[str]:
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []
    body = text[1:-1].strip()
    if not body:
        return []
    out: list[str] = []
    for token in body.split(","):
        cleaned = token.strip().strip('"').strip("'")
        if cleaned:
            out.append(cleaned)
    return out


def _parse_bool(value: str, default: bool = True) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    return default


def _apply_test_key(entry: dict[str, Any], key: str, value: str) -> None:
    normalized_key = key.strip()
    cleaned = value.strip()
    if normalized_key == "path":
        entry["path"] = cleaned.strip('"').strip("'")
    elif normalized_key == "scope":
        entry["scope"] = cleaned.strip('"').strip("'") or "file"
    elif normalized_key == "framework":
        entry["framework"] = cleaned.strip('"').strip("'") or "pytest"
    elif normalized_key == "pattern":
        entry["pattern"] = cleaned.strip('"').strip("'") or "test_*.py"
    elif normalized_key == "recursive":
        entry["recursive"] = _parse_bool(cleaned, default=True)
    elif normalized_key == "mcp_profiles":
        entry["mcp_profiles"] = _parse_inline_list(cleaned)


def _parse_verifier_yaml(text: str) -> dict[str, Any]:
    schema_version = SCHEMA_VERSION
    tests: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    collecting_mcp = False

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("schema_version:"):
            value = stripped.split(":", 1)[1].strip()
            try:
                schema_version = int(value)
            except ValueError:
                schema_version = SCHEMA_VERSION
            continue

        if stripped == "tests:":
            continue

        if stripped.startswith("- "):
            current = _new_test_entry()
            tests.append(current)
            collecting_mcp = False
            remainder = stripped[2:].strip()
            if ":" in remainder:
                key, value = remainder.split(":", 1)
                _apply_test_key(current, key, value)
            continue

        if current is None:
            continue

        if stripped.startswith("mcp_profiles:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                current["mcp_profiles"] = _parse_inline_list(value)
                collecting_mcp = False
            else:
                current["mcp_profiles"] = []
                collecting_mcp = True
            continue

        if collecting_mcp and stripped.startswith("- "):
            value = stripped[2:].strip().strip('"').strip("'")
            if value:
                current.setdefault("mcp_profiles", []).append(value)
            continue

        collecting_mcp = False
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            _apply_test_key(current, key, value)

    return {"schema_version": schema_version, "tests": tests}


def ensure_verifier_file(paths: RepoPaths) -> None:
    if paths.verifier_file.exists():
        return
    paths.verifier_file.parent.mkdir(parents=True, exist_ok=True)
    paths.verifier_file.write_text(verifier_template_text(), encoding="utf-8")


def load_verifier_config(paths: RepoPaths) -> VerifierConfig:
    ensure_verifier_file(paths)
    raw_text = paths.verifier_file.read_text(encoding="utf-8")
    raw = _parse_verifier_yaml(raw_text)
    if not isinstance(raw, dict):
        raw = default_verifier_payload()

    normalized_tests: list[dict[str, Any]] = []
    for item in raw.get("tests", []):
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("path", "")).strip()
        if not path_value:
            continue
        normalized_tests.append(
            {
                "path": path_value,
                "scope": str(item.get("scope", "file") or "file"),
                "framework": str(item.get("framework", "pytest") or "pytest"),
                "pattern": str(item.get("pattern", "test_*.py") or "test_*.py"),
                "recursive": bool(item.get("recursive", True)),
                "mcp_profiles": [str(value) for value in item.get("mcp_profiles", []) if str(value).strip()],
            }
        )

    payload = {"schema_version": int(raw.get("schema_version", SCHEMA_VERSION)), "tests": normalized_tests}
    return VerifierConfig.model_validate(payload)


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
    write_json(paths.autocheck_map_file, map_payload)
    return {
        "ok": True,
        "autocheck_map": str(paths.autocheck_map_file),
        "link_count": len(map_payload.get("links", [])),
        "target_count": len(map_payload.get("targets", [])),
    }


def sync_feature_list_from_verifier(paths: RepoPaths) -> dict[str, Any]:
    # Backward-compatible alias. It now syncs only autocheck_map from verifier links.
    return sync_autocheck_map_from_verifier(paths)


def mapped_target_ids(paths: RepoPaths) -> set[str]:
    payload = read_json(paths.autocheck_map_file, {"targets": []})
    target_ids: set[str] = set()
    for item in payload.get("targets", []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target", "")).strip()
        if target:
            target_ids.add(target)
    return target_ids


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


def _extract_pytest_targets_from_text(text: str) -> set[str]:
    targets: set[str] = set()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()

    in_pytest_cmd = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {";", "&&", "||", "|"}:
            in_pytest_cmd = False
            index += 1
            continue
        if token == "pytest":
            in_pytest_cmd = True
            index += 1
            continue
        if not in_pytest_cmd:
            index += 1
            continue

        if token.startswith("-"):
            if token in PYTEST_OPTIONS_WITH_VALUE and index + 1 < len(tokens):
                index += 2
            else:
                index += 1
            continue

        candidate = token.strip().strip(",")
        if not ("/" in candidate or ".py" in candidate or "::" in candidate):
            index += 1
            continue
        target_check = validate_pytest_target(candidate)
        if target_check.allowed:
            targets.add(candidate)
        index += 1

    return targets


def _task_targets_from_criteria(criteria: list[str], linked_targets: set[str]) -> tuple[set[str], set[str]]:
    referenced: set[str] = set()
    unlinked: set[str] = set()

    for item in criteria:
        text = str(item or "").strip()
        if not text:
            continue

        parsed_targets = _extract_pytest_targets_from_text(text)
        for target in parsed_targets:
            if target in linked_targets:
                referenced.add(target)
            else:
                unlinked.add(target)

        for token in re.findall(r"[A-Za-z0-9_./:-]+", text):
            normalized = token.strip().strip("`")
            if normalized in linked_targets:
                referenced.add(normalized)

    return referenced, unlinked


def _load_feature_payload(paths: RepoPaths) -> dict[str, Any]:
    return read_json(
        paths.rpi_dir / "feature_list.json",
        {
            "schema_version": SCHEMA_VERSION,
            "template": {"id": "rpi_feature_list", "version": "2.2.0"},
            "sub_tasks": [],
        },
    )


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

    for task in feature_payload.get("sub_tasks", []):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            continue
        criteria = [str(entry) for entry in task.get("criteria", [])]
        task_targets, unlinked = _task_targets_from_criteria(criteria, linked_targets)
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

    map_payload = read_json(paths.autocheck_map_file, {"schema_version": SCHEMA_VERSION, "links": [], "targets": []})
    target_entries = [item for item in map_payload.get("targets", []) if isinstance(item, dict)]
    target_index = {str(item.get("target", "")): item for item in target_entries if str(item.get("target", "")).strip()}
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
        target_id = str(target_meta.get("target_id", _target_id(target)))
        mcp_profiles = [str(value) for value in target_meta.get("mcp_profiles", []) if str(value).strip()]

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
                "kind": str(target_meta.get("kind", "")),
                "path": str(target_meta.get("path", "")),
                "node_id": str(target_meta.get("node_id", "")),
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

        for task in feature_payload.get("sub_tasks", []):
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id", "")).strip()
            if not task_id:
                continue

            criteria = [str(entry) for entry in task.get("criteria", [])]
            task_targets, unlinked_refs = _task_targets_from_criteria(criteria, linked_targets)
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
