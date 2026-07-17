"""The one synchronous execution lifecycle shared by every interface."""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .artifacts import index_execution_artifacts, index_report_artifacts
from .preflight import require_preflight, standard_preflight
from .provenance import (
    create_trigger,
    external_input_identity,
    inventory_external_inputs,
    record_external_inputs,
)
from .reports import write_report_bundle
from .runner import (
    RUN_CONTEXT,
    compute_hashes,
    find_duplicate_output_run,
    hash_run_output,
    hash_json,
    local_run_context,
    run_script,
    run_script_local,
    scrub_secrets,
)
from .runs import (
    TERMINAL_STATUSES,
    allocate_run,
    clear_worker,
    finalize_canceled,
    finalize_failure,
    finalize_success,
    get_run,
    mark_running,
    register_worker,
)
from .snapshots import (
    capture_workspace,
    get_snapshot,
    materialize_snapshot,
    snapshot_hashes,
)
from .store import init_db, require_autoexp_git_repo
from .workspace import materialize_workspace, resolve_root, run_dir_for, script_manifest, write_json


def _source_request(root, run_id, snapshot_id):
    if run_id and snapshot_id:
        raise ValueError("run_id and snapshot_id are mutually exclusive")
    if run_id:
        parent = get_run(run_id, root)
        if parent["status"] not in TERMINAL_STATUSES:
            raise ValueError(f"run {run_id} is not terminal")
        if not parent.get("source_snapshot_id"):
            raise ValueError(f"run {run_id} has no source snapshot")
        return get_snapshot(parent["source_snapshot_id"], root), parent
    if snapshot_id:
        return get_snapshot(snapshot_id, root), None
    return None, None


def preflight_request(root=None, run_id=None, snapshot_id=None):
    """Check an execution request without allocating a run or trigger."""
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    init_db(root)
    snapshot, _ = _source_request(root, run_id, snapshot_id)
    with tempfile.TemporaryDirectory(prefix="autoexp-preflight-") as tmp:
        if snapshot is None:
            materialize_workspace(root, tmp)
        else:
            materialize_snapshot(snapshot["snapshot_id"], tmp, root)
        return standard_preflight(root, tmp)


def _index_and_hash(run_id, run_dir, root):
    errors = []
    try:
        index_execution_artifacts(run_id, root)
        index_report_artifacts(run_id, root)
    except Exception as exc:
        errors.append(f"artifact indexing failed: {exc}")
    try:
        output_hash = hash_run_output(run_dir)
    except Exception as exc:
        output_hash = ""
        errors.append(f"output hashing failed: {exc}")
    return output_hash, errors


def _finalize_setup_failure(run, root, exc, runner, runner_identity):
    """Turn any post-allocation setup error into inspectable evidence."""
    if run["status"] == "queued":
        run = mark_running(run["run_id"], runner, runner_identity, root)
    run_dir = run_dir_for(run, root)
    output_hash, errors = _index_and_hash(run["run_id"], run_dir, root)
    message = "; ".join([str(exc), *errors])
    return finalize_failure(
        run["run_id"],
        output_hash,
        0,
        root,
        failure_kind="setup_error",
        failure_message=message,
    )


def execute(
    root=None,
    run_id=None,
    snapshot_id=None,
    *,
    trigger_kind="cli",
    actor_name=None,
    session_id=None,
    request_id=None,
    metadata=None,
    environment=None,
    timeout_sec=None,
):
    """Execute current source, a snapshot, or a historical run as a new run."""
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    init_db(root)
    snapshot, parent = _source_request(root, run_id, snapshot_id)

    preflight = None
    trigger = None
    runs_root = root / "runs"
    if (
        runs_root.is_symlink()
        or not runs_root.is_dir()
        or not runs_root.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("runs directory must stay inside the autoexp project")
    temp_root = Path(tempfile.mkdtemp(prefix=".autoexp-run-", dir=runs_root))
    allocated = None
    try:
        if snapshot is None:
            trigger = create_trigger(
                trigger_kind,
                root=root,
                actor_name=actor_name,
                session_id=session_id,
                request_id=request_id,
                metadata=metadata,
            )
            snapshot = capture_workspace(
                root,
                created_by_trigger_id=trigger["trigger_id"],
                label="Execution source",
            )

        materialize_snapshot(snapshot["snapshot_id"], temp_root, root)
        if preflight is None:
            preflight = require_preflight(root, temp_root)
        hashes = compute_hashes(temp_root)
        input_records = inventory_external_inputs(temp_root, root, environment)
        hashes["capsule_hash"] = hash_json({
            "execution": hashes["capsule_hash"],
            "external_inputs": external_input_identity(input_records),
        })
        if snapshot_hashes(temp_root)["source_hash"] != snapshot["source_hash"]:
            raise ValueError("materialized source does not match its snapshot identity")

        if trigger is None:
            trigger = create_trigger(
                trigger_kind,
                root=root,
                actor_name=actor_name,
                session_id=session_id,
                request_id=request_id,
                metadata=metadata,
            )

        for name in ("output", "logs", "report"):
            (temp_root / name).mkdir()
        allocated = allocate_run(
            source_snapshot_id=snapshot["snapshot_id"],
            hashes=hashes,
            stage_commit=snapshot["git_commit"],
            root=root,
            parent_run_id=parent["run_id"] if parent else None,
            trigger_id=trigger["trigger_id"],
            script_name_value=script_manifest(temp_root).get("name"),
            source_root=temp_root,
        )
        register_worker(allocated["run_id"], root)
        run_dir = run_dir_for(allocated, root)
        temp_root.rename(run_dir)
        temp_root = None

        record_external_inputs(allocated["run_id"], input_records, root)
        context = RUN_CONTEXT if preflight["runner"] == "docker" else local_run_context(
            run_dir, run_dir, root
        )
        write_json(run_dir / "ctx.json", context)
        running = mark_running(
            allocated["run_id"],
            preflight["runner"],
            preflight["runner_identity"],
            root,
        )
    except Exception as exc:
        if allocated:
            try:
                return _finalize_setup_failure(
                    get_run(allocated["run_id"], root),
                    root,
                    exc,
                    (preflight or {}).get("runner") or "unknown",
                    (preflight or {}).get("runner_identity") or "unknown",
                )
            finally:
                clear_worker(allocated["run_id"], root)
        raise
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)

    started = time.monotonic()
    code = None
    runner_error = None
    canceled = False
    try:
        adapter = run_script if running["runner"] == "docker" else run_script_local
        code = adapter(
            run_dir,
            root=root,
            source_root=run_dir,
            extra_env=environment,
            timeout_sec=timeout_sec,
        )
    except KeyboardInterrupt:
        canceled = True
    except Exception as exc:
        runner_error = exc
    try:
        scrub_secrets(run_dir, root, environment)
    except Exception:
        for name in ("output", "logs", "report"):
            shutil.rmtree(run_dir / name, ignore_errors=True)
            (run_dir / name).mkdir()
        (run_dir / "logs" / "script.stderr.log").write_text("evidence removed because secret scrubbing failed\n")
        runner_error = ValueError("secret scrubbing failed")

    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    output_hash, evidence_errors = _index_and_hash(running["run_id"], run_dir, root)
    source_error = None
    try:
        if snapshot_hashes(run_dir)["source_hash"] != snapshot["source_hash"]:
            source_error = "runner modified its pinned source"
    except Exception as exc:
        source_error = f"source verification failed: {exc}"

    bundle_error = None
    try:
        write_report_bundle(running["run_id"], root)
    except Exception as exc:
        bundle_error = f"report bundle failed: {exc}"

    if canceled:
        run = finalize_canceled(
            running["run_id"],
            output_hash,
            duration_ms,
            root,
            exit_code=code,
        )
    elif source_error or evidence_errors or bundle_error or runner_error is not None:
        messages = [
            *( [source_error] if source_error else [] ),
            *evidence_errors,
            *( [bundle_error] if bundle_error else [] ),
            *( [str(runner_error)] if runner_error is not None else [] ),
        ]
        kind = (
            "source_mutation" if source_error
            else "artifact_error" if evidence_errors
            else "report_bundle_error" if bundle_error
            else "timeout" if isinstance(runner_error, subprocess.TimeoutExpired)
            else "runner_error"
        )
        run = finalize_failure(
            running["run_id"],
            output_hash,
            duration_ms,
            root,
            exit_code=code,
            failure_kind=kind,
            failure_message="; ".join(messages),
        )
    elif code != 0:
        run = finalize_failure(
            running["run_id"],
            output_hash,
            duration_ms,
            root,
            exit_code=code,
            failure_kind="process_exit",
            failure_message=f"experiment exited with status {code}",
        )
    else:
        reproduced = find_duplicate_output_run(hashes, output_hash, root)
        run = finalize_success(
            running["run_id"],
            output_hash,
            duration_ms,
            root,
            reproduces_run_id=reproduced["run_id"] if reproduced else None,
        )

    try:
        if bundle_error is None:
            write_report_bundle(run["run_id"], root)
        return get_run(run["run_id"], root)
    finally:
        clear_worker(run["run_id"], root)
