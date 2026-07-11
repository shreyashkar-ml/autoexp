import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .store import autoexp_git, db, git_status, insert_run, require_autoexp_git_repo
from .workspace import (
    PROJECT_CONFIG,
    die,
    now,
    read_json,
    resolve_root,
    run_dir_for,
    script_manifest,
    source_paths,
    write_json,
)


TERMINAL_STATUSES = {"success", "failed", "canceled"}


def _worker_marker(run_id, root):
    return Path(root) / ".autoexp" / "workers" / f"{run_id}.pid"


def register_worker(run_id, root=None):
    root = resolve_root(root)
    marker = _worker_marker(run_id, root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(os.getpid()))


def clear_worker(run_id, root=None):
    _worker_marker(run_id, resolve_root(root)).unlink(missing_ok=True)


def _worker_alive(run_id, root):
    marker = _worker_marker(run_id, root)
    try:
        pid = int(marker.read_text().strip())
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return False


def script_name(run_id, root=None):
    """The script's display name, falling back to a per-run name when generic."""
    name = script_manifest(root).get("name", "").strip()
    return name if name and name != "script" else f"script-{run_id}"


def get_run(run_id, root=None):
    """Load a run row from the index, or its run.json snapshot as a fallback."""
    root = resolve_root(root)
    conn = db(root)
    row = conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
    conn.close()
    if row:
        run = dict(row)
        run["stage_status"] = json.loads(run["stage_status"])
        return run
    if Path(run_id).name != run_id:
        raise ValueError(f"unknown run_id: {run_id}")
    path = run_dir_for({"run_id": run_id}, root) / "run.json"
    if path.exists():
        return read_json(path)
    raise ValueError(f"unknown run_id: {run_id}")


def run_stage_commit(run):
    commit = run.get("stage_commit")
    if not commit:
        raise ValueError(f"{run.get('run_id', 'run')} does not record a restorable stage commit")
    return commit


def copy_run_source(src_root, run_root):
    """Snapshot script/ plus the rest of the source set into a run directory."""
    src_root = Path(src_root)
    run_root = Path(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    script_target = run_root / "script"
    if (src_root / "script").is_symlink() or script_target.is_symlink():
        raise ValueError("script directory must not be a symlink")
    if any(path.is_symlink() for path in (src_root / "script").rglob("*")):
        raise ValueError("script source must not contain symlinks")
    if script_target.exists():
        shutil.rmtree(script_target)
    shutil.copytree(src_root / "script", script_target, symlinks=True)
    for path in source_paths(src_root)[1:]:  # [0] is "script", already copied above
        source = src_root / path
        target = run_root / path
        if (
            source.is_symlink()
            or target.is_symlink()
            or not source.resolve().is_relative_to(src_root.resolve())
            or not target.resolve(strict=False).is_relative_to(run_root.resolve())
        ):
            raise ValueError(f"source path must not be a symlink: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def source_root_for_run(run, root=None):
    """Where this run's source lives: its own snapshot if intact, else the project."""
    root = resolve_root(root)
    run_root = run_dir_for(run, root)
    has_snapshot = (run_root / "script").is_dir() and (run_root / PROJECT_CONFIG).is_file()
    return run_root if has_snapshot else root


def restore_run_state(run_id, root=None):
    """Restore script/config from a run, refusing to clobber uncommitted changes."""
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    if git_status(source_paths(root), root=root):
        die("refusing to restore run state over uncommitted script/config changes")
    run = get_run(run_id, root)
    if run.get("source_snapshot_id"):
        from .snapshots import materialize_snapshot

        with tempfile.TemporaryDirectory(prefix="autoexp-restore-") as tmp:
            materialize_snapshot(run["source_snapshot_id"], tmp, root)
            copy_run_source(tmp, root)
    else:
        autoexp_git(["checkout", run_stage_commit(run), "--", *source_paths(root)], root=root)
    return run, run_stage_commit(run)


def new_run_id(hashes, root):
    """A unique, sortable run id: timestamp + capsule prefix + random suffix."""
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
    *,
    source_snapshot_id,
    hashes,
    stage_commit,
    root=None,
    parent_run_id=None,
    trigger_id=None,
    script_name_value=None,
    source_root=None,
):
    """Create one queued execution record. Every call allocates a new identity."""
    root = resolve_root(root)
    run_id, created_at = new_run_id(hashes, root)
    run = {
        "run_id": run_id,
        "run_dir": f"runs/{run_id}",
        "report_path": "",
        "output_hash": "",
        **hashes,
        "script_name": (
            script_name_value
            if script_name_value and script_name_value != "script"
            else script_name(run_id, source_root or root)
        ),
        "stage_commit": stage_commit,
        "source_snapshot_id": source_snapshot_id,
        "parent_run_id": parent_run_id,
        "trigger_id": trigger_id,
        "status": "queued",
        "stage_status": {"script": "queued"},
        "created_at": created_at,
    }
    insert_run(run, root)
    stored = get_run(run_id, root)
    _persist_run_json(stored, root)
    return stored


def _transition(run_id, expected, updates, root=None):
    root = resolve_root(root)
    conn = db(root)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = [
        json.dumps(value) if key == "stage_status" else value
        for key, value in updates.items()
    ]
    cursor = conn.execute(
        f"update runs set {assignments} where run_id = ? and status = ?",
        (*values, run_id, expected),
    )
    conn.commit()
    if cursor.rowcount != 1:
        row = conn.execute("select status from runs where run_id = ?", (run_id,)).fetchone()
        conn.close()
        actual = row["status"] if row else "missing"
        raise ValueError(f"run {run_id} must be {expected}, not {actual}")
    conn.close()
    run = get_run(run_id, root)
    _persist_run_json(run, root)
    return run


def mark_running(run_id, runner, runner_identity, root=None, *, started_at=None):
    return _transition(
        run_id,
        "queued",
        {
            "status": "running",
            "stage_status": {"script": "running"},
            "runner": runner,
            "runner_identity": runner_identity,
            "started_at": started_at or now(),
        },
        root,
    )


def finalize_success(
    run_id,
    output_hash,
    duration_ms,
    root=None,
    *,
    exit_code=0,
    ended_at=None,
    reproduces_run_id=None,
):
    if exit_code != 0:
        raise ValueError("successful runs must have exit_code 0")
    return _transition(
        run_id,
        "running",
        {
            "status": "success",
            "stage_status": {"script": "success"},
            "exit_code": exit_code,
            "output_hash": output_hash,
            "ended_at": ended_at or now(),
            "duration_ms": duration_ms,
            "failure_kind": None,
            "failure_message": None,
            "reproduces_run_id": reproduces_run_id,
        },
        root,
    )


def finalize_failure(
    run_id,
    output_hash,
    duration_ms,
    root=None,
    *,
    exit_code=None,
    ended_at=None,
    failure_kind="process_exit",
    failure_message=None,
):
    label = str(exit_code) if exit_code is not None else failure_kind
    return _transition(
        run_id,
        "running",
        {
            "status": "failed",
            "stage_status": {"script": f"failed:{label}"},
            "exit_code": exit_code,
            "output_hash": output_hash,
            "ended_at": ended_at or now(),
            "duration_ms": duration_ms,
            "failure_kind": failure_kind,
            "failure_message": failure_message,
            "reproduces_run_id": None,
        },
        root,
    )


def finalize_canceled(
    run_id,
    output_hash,
    duration_ms,
    root=None,
    *,
    exit_code=None,
    ended_at=None,
    failure_message="execution canceled",
):
    return _transition(
        run_id,
        "running",
        {
            "status": "canceled",
            "stage_status": {"script": "canceled"},
            "exit_code": exit_code,
            "output_hash": output_hash,
            "ended_at": ended_at or now(),
            "duration_ms": duration_ms,
            "failure_kind": "canceled",
            "failure_message": failure_message,
            "reproduces_run_id": None,
        },
        root,
    )


def recover_stranded_runs(root=None, *, canceled=False, run_id=None):
    """Finalize nonterminal rows left behind by a dead worker or server restart."""
    from .artifacts import index_execution_artifacts
    from .runner import hash_run_output

    root = resolve_root(root)
    conn = db(root)
    rows = conn.execute(
        """select run_id from runs where status in ('queued', 'running')
           and (? is null or run_id = ?) order by created_at, rowid""",
        (run_id, run_id),
    ).fetchall()
    conn.close()
    recovered = []
    for row in rows:
        if _worker_alive(row["run_id"], root):
            continue
        run = get_run(row["run_id"], root)
        if run["status"] == "queued":
            run = mark_running(
                run["run_id"],
                run.get("runner") or "unknown",
                run.get("runner_identity") or "unknown",
                root,
                started_at=run.get("started_at") or run["created_at"],
            )
        message = "execution canceled" if canceled else "execution interrupted before completion"
        try:
            index_execution_artifacts(run["run_id"], root)
        except Exception as exc:
            message = f"{message}; artifact indexing failed: {exc}"
        try:
            output_hash = hash_run_output(run_dir_for(run, root))
        except Exception as exc:
            output_hash = ""
            message = f"{message}; output hashing failed: {exc}"
        finalize = finalize_canceled if canceled else finalize_failure
        kwargs = {"failure_message": message}
        if not canceled:
            kwargs["failure_kind"] = "interrupted"
        recovered.append(finalize(
            run["run_id"],
            output_hash,
            0,
            root,
            exit_code=run.get("exit_code"),
            **kwargs,
        ))
        clear_worker(run["run_id"], root)
    return recovered
