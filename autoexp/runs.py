import filecmp
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .store import db, insert_run
from .workspace import (
    ensure_within_project,
    now,
    read_json,
    repository_root,
    resolve_root,
    run_dir_for,
    safe_repository_path,
    script_manifest,
    write_json,
    experiment_id,
)


TERMINAL_STATUSES = {"success", "failed", "canceled"}


def _worker_marker(run_id, root):
    return Path(root) / "workers" / f"{run_id}.pid"


def register_worker(run_id, root=None):
    root = resolve_root(root)
    marker = _worker_marker(run_id, root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(os.getpid()))


def clear_worker(run_id, root=None):
    _worker_marker(run_id, resolve_root(root)).unlink(missing_ok=True)


def _worker_alive(run_id, root):
    try:
        pid = int(_worker_marker(run_id, root).read_text().strip())
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return False


def script_name(run_id, root=None):
    name = script_manifest(root).get("name", "").strip()
    return name if name and name != "script" else f"script-{run_id}"


def get_run(run_id, root=None):
    root = resolve_root(root)
    conn = db()
    row = conn.execute(
        "select * from runs where run_id = ? and experiment_id = ?",
        (run_id, experiment_id(root)),
    ).fetchone()
    conn.close()
    if row:
        run = dict(row)
        run["stage_status"] = json.loads(run["stage_status"])
        return run
    raise ValueError(f"unknown run_id: {run_id}")


def run_stage_commit(run):
    commit = run.get("stage_commit")
    if not commit:
        raise ValueError(f"{run.get('run_id', 'run')} has no restorable snapshot")
    return commit


def copy_run_source(src_root, root, *, only=None):
    """Restore declared source files from a snapshot to the live repository."""
    src_root = Path(src_root).resolve()
    root = resolve_root(root)
    only = (
        {ensure_within_project(path, "snapshot source path") for path in only}
        if only is not None else None
    )
    config = read_json(src_root / ".autoexp/project.json")
    for item in config.get("files", []):
        if item.get("role") in {"secret-source", "generated-output", "frozen-evaluator"}:
            continue
        rel = ensure_within_project(item["path"], "snapshot source path")
        if only is not None and rel not in only:
            continue
        source = src_root / rel
        cursor = src_root
        for part in rel.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError(f"snapshot source path contains a symlink: {rel}")
        if not source.resolve(strict=False).is_relative_to(src_root):
            raise ValueError(f"snapshot source path escapes snapshot: {rel}")
        target = safe_repository_path(root, rel)
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif source.exists():
            raise ValueError(f"snapshot source is not a file: {rel}")
        elif target.is_dir():
            raise ValueError(f"refusing to replace directory with absent source: {rel}")
        else:
            target.unlink(missing_ok=True)


def _dirty_restore_paths(src_root, root):
    src_root = Path(src_root)
    config = read_json(src_root / ".autoexp/project.json")
    paths = [
        str(ensure_within_project(item["path"], "snapshot source path"))
        for item in config.get("files", [])
        if item.get("role") not in {"secret-source", "generated-output", "frozen-evaluator"}
    ]
    if not paths:
        return ""
    proc = subprocess.run(
        ["git", "-C", str(repository_root(root)), "status", "--porcelain", "--", *paths],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.stdout.strip():
        return proc.stdout.strip()
    ignored = subprocess.run(
        ["git", "-C", str(repository_root(root)), "check-ignore", "-z", "--stdin"],
        input="\0".join(paths) + "\0", stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    if ignored.returncode not in (0, 1):
        ignored.check_returncode()
    ignored_paths = set(ignored.stdout.split("\0"))
    changed = []
    for raw in paths:
        if raw not in ignored_paths:
            continue
        source = src_root / raw
        target = safe_repository_path(root, raw)
        if target.exists() and not (
            source.is_file()
            and target.is_file()
            and filecmp.cmp(source, target, shallow=False)
        ):
            changed.append(f"!! {raw}")
    return "\n".join(changed)


def source_root_for_run(run, root=None):
    root = resolve_root(root)
    run_root = run_dir_for(run, root)
    if not (run_root / ".autoexp/project.json").is_file():
        raise FileNotFoundError(f"missing immutable run source: {run['run_id']}")
    return run_root


def restore_run_state(run_id, root=None):
    root = resolve_root(root)
    run = get_run(run_id, root)
    if not run.get("source_snapshot_id"):
        raise ValueError(f"run {run_id} has no source snapshot")
    from .snapshots import materialize_snapshot

    with tempfile.TemporaryDirectory(prefix="autoexp-restore-") as tmp:
        materialize_snapshot(run["source_snapshot_id"], tmp, root)
        dirty = _dirty_restore_paths(tmp, root)
        if dirty:
            raise ValueError(
                "refusing to restore over uncommitted source changes:\n"
                f"{dirty}"
            )
        copy_run_source(tmp, root)
    return run, run_stage_commit(run)


def new_run_id(hashes, root):
    created_at = now()
    while True:
        run_id = f"{created_at}_{hashes['capsule_hash'][:8]}_{uuid.uuid4().hex[:6]}"
        if not run_dir_for({"run_id": run_id}, root).exists():
            return run_id, created_at


def _persist_run_json(run, root):
    path = run_dir_for(run, root) / "run.json"
    if path.parent.is_dir():
        write_json(path, run)


def allocate_run(
    *, source_snapshot_id, hashes, stage_commit, root=None, parent_run_id=None,
    trigger_id=None, script_name_value=None, source_root=None,
):
    root = resolve_root(root)
    run_id, created_at = new_run_id(hashes, root)
    run = {
        "run_id": run_id, "run_dir": f"runs/{run_id}", "report_path": "",
        "output_hash": "", **hashes,
        "script_name": script_name_value or script_name(run_id, source_root or root),
        "stage_commit": stage_commit, "source_snapshot_id": source_snapshot_id,
        "parent_run_id": parent_run_id, "trigger_id": trigger_id,
        "status": "queued", "stage_status": {"script": "queued"},
        "created_at": created_at,
    }
    insert_run(run, root)
    stored = get_run(run_id, root)
    _persist_run_json(stored, root)
    return stored


def _transition(run_id, expected, updates, root=None):
    root = resolve_root(root)
    conn = db()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = [json.dumps(value) if key == "stage_status" else value for key, value in updates.items()]
    cursor = conn.execute(
        f"update runs set {assignments} where run_id = ? and experiment_id = ? and status = ?",
        (*values, run_id, experiment_id(root), expected),
    )
    conn.commit()
    if cursor.rowcount != 1:
        row = conn.execute("select status from runs where run_id = ?", (run_id,)).fetchone()
        conn.close()
        raise ValueError(f"run {run_id} must be {expected}, not {row['status'] if row else 'missing'}")
    conn.close()
    run = get_run(run_id, root)
    _persist_run_json(run, root)
    return run


def mark_running(run_id, runner, runner_identity, root=None, *, started_at=None):
    return _transition(run_id, "queued", {
        "status": "running", "stage_status": {"script": "running"},
        "runner": runner, "runner_identity": runner_identity,
        "started_at": started_at or now(),
    }, root)


def finalize_success(run_id, output_hash, duration_ms, root=None, *, exit_code=0, ended_at=None, reproduces_run_id=None):
    if exit_code != 0:
        raise ValueError("successful runs must have exit_code 0")
    return _transition(run_id, "running", {
        "status": "success", "stage_status": {"script": "success"},
        "exit_code": 0, "output_hash": output_hash, "ended_at": ended_at or now(),
        "duration_ms": duration_ms, "failure_kind": None, "failure_message": None,
        "reproduces_run_id": reproduces_run_id,
    }, root)


def finalize_failure(run_id, output_hash, duration_ms, root=None, *, exit_code=None, ended_at=None, failure_kind="process_exit", failure_message=None):
    from .runner import redact_secrets
    failure_message = redact_secrets(failure_message or "", resolve_root(root))
    label = str(exit_code) if exit_code is not None else failure_kind
    return _transition(run_id, "running", {
        "status": "failed", "stage_status": {"script": f"failed:{label}"},
        "exit_code": exit_code, "output_hash": output_hash, "ended_at": ended_at or now(),
        "duration_ms": duration_ms, "failure_kind": failure_kind,
        "failure_message": failure_message, "reproduces_run_id": None,
    }, root)


def finalize_canceled(run_id, output_hash, duration_ms, root=None, *, exit_code=None, ended_at=None, failure_message="execution canceled"):
    from .runner import redact_secrets
    failure_message = redact_secrets(failure_message or "", resolve_root(root))
    return _transition(run_id, "running", {
        "status": "canceled", "stage_status": {"script": "canceled"},
        "exit_code": exit_code, "output_hash": output_hash, "ended_at": ended_at or now(),
        "duration_ms": duration_ms, "failure_kind": "canceled",
        "failure_message": failure_message, "reproduces_run_id": None,
    }, root)


def recover_stranded_runs(root=None, *, canceled=False, run_id=None):
    from .artifacts import index_execution_artifacts
    from .runner import hash_run_output

    root = resolve_root(root)
    conn = db()
    rows = conn.execute(
        """select run_id from runs where experiment_id = ?
           and status in ('queued', 'running') and (? is null or run_id = ?)
           order by created_at, rowid""",
        (experiment_id(root), run_id, run_id),
    ).fetchall()
    conn.close()
    recovered = []
    for row in rows:
        if _worker_alive(row["run_id"], root):
            continue
        run = get_run(row["run_id"], root)
        if run["status"] == "queued":
            run = mark_running(run["run_id"], run.get("runner") or "unknown", run.get("runner_identity") or "unknown", root)
        message = "execution canceled" if canceled else "execution interrupted before completion"
        try:
            index_execution_artifacts(run["run_id"], root)
            output_hash = hash_run_output(run_dir_for(run, root))
        except Exception as exc:
            output_hash = ""
            message = f"{message}; evidence recovery failed: {exc}"
        finalize = finalize_canceled if canceled else finalize_failure
        kwargs = {"failure_message": message}
        if not canceled:
            kwargs["failure_kind"] = "interrupted"
        recovered.append(finalize(run["run_id"], output_hash, 0, root, exit_code=run.get("exit_code"), **kwargs))
        clear_worker(run["run_id"], root)
    return recovered
