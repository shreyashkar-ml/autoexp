import json
import shutil
import uuid
from pathlib import Path

from .store import autoexp_git, db, git_status, require_autoexp_git_repo
from .workspace import (
    PROJECT_CONFIG,
    die,
    now,
    read_json,
    resolve_root,
    run_dir_for,
    script_manifest,
    source_paths,
)


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
        die(f"unknown run_id: {run_id}")
    path = root / "runs" / run_id / "run.json"
    if path.exists():
        return read_json(path)
    die(f"unknown run_id: {run_id}")


def run_stage_commit(run):
    commit = run.get("stage_commit")
    if not commit:
        die(f"{run.get('run_id', 'run')} does not record a restorable stage commit")
    return commit


def copy_run_source(src_root, run_root):
    """Snapshot script/ plus the rest of the source set into a run directory."""
    run_root.mkdir(parents=True, exist_ok=True)
    script_target = run_root / "script"
    if script_target.exists():
        shutil.rmtree(script_target)
    shutil.copytree(Path(src_root) / "script", script_target)
    for path in source_paths(src_root)[1:]:  # [0] is "script", already copied above
        target = run_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(src_root) / path, target)


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
    source_root = source_root_for_run(run, root)
    if source_root != root:
        copy_run_source(source_root, root)
    else:
        autoexp_git(["checkout", run_stage_commit(run), "--", *source_paths(root)], root=root)
    return run, run_stage_commit(run)


def new_run_id(hashes, root):
    """A unique, sortable run id: timestamp + capsule prefix + random suffix."""
    created_at = now()
    while True:
        run_id = f"{created_at}_{hashes['capsule_hash'][:8]}_{uuid.uuid4().hex[:6]}"
        if not (root / "runs" / run_id).exists():
            return run_id, created_at
