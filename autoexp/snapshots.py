"""Immutable source snapshot capture, derivation, and materialization."""

import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from .git_store import commit_source_tree, materialize_commit, preserve_snapshot_ref
from .store import autoexp_git, current_autoexp_commit, db, git_commit_source
from .workspace import (
    PROJECT_CONFIG,
    ensure_within_project,
    now,
    project_id,
    read_json,
    resolve_root,
    source_paths,
)


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _hash_file(path):
    return _hash_bytes(path.read_bytes()) if path.is_file() else _hash_bytes(b"")


def _hash_script_source(script_dir):
    digest = hashlib.sha256()
    excluded = {"stage.json", "params.json", "params.schema.json"}
    if script_dir.is_dir():
        for path in sorted(script_dir.rglob("*")):
            if not path.is_file() or path.relative_to(script_dir).as_posix() in excluded:
                continue
            digest.update(path.relative_to(script_dir).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def snapshot_hashes(source_root):
    """Return semantic hashes for the execution-relevant source categories."""
    source_root = Path(source_root)
    config = read_json(source_root / PROJECT_CONFIG)
    runtime_config = {
        key: config.get(key)
        for key in ("runner", "sandbox", "runtime")
        if key in config
    }
    hashes = {
        "script_hash": _hash_script_source(source_root / "script"),
        "params_hash": _hash_file(source_root / "script" / "params.json"),
        "manifest_hash": _hash_file(source_root / "script" / "stage.json"),
        "runtime_config_hash": _hash_bytes(
            json.dumps(runtime_config, sort_keys=True, separators=(",", ":")).encode()
        ),
    }
    hashes["source_hash"] = _hash_bytes(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    )
    return hashes


def _insert_snapshot(
    commit,
    source_root,
    *,
    root,
    parent_snapshot_id=None,
    created_at=None,
    label=None,
    legacy_run_id=None,
):
    hashes = snapshot_hashes(source_root)
    snapshot_id = f"snap_{hashes['source_hash'][:8]}_{uuid.uuid4().hex[:6]}"
    snapshot = {
        "snapshot_id": snapshot_id,
        "project_id": project_id(root),
        "parent_snapshot_id": parent_snapshot_id,
        "git_commit": commit,
        **hashes,
        "created_at": created_at or now(),
        "created_by_trigger_id": None,
        "label": label,
        "legacy_run_id": legacy_run_id,
    }
    preserve_snapshot_ref(snapshot_id, commit, root)
    conn = db(root)
    conn.execute(
        """insert into source_snapshots(
            snapshot_id, project_id, parent_snapshot_id, git_commit,
            script_hash, params_hash, manifest_hash, runtime_config_hash,
            source_hash, created_at, created_by_trigger_id, label, legacy_run_id
        ) values(
            :snapshot_id, :project_id, :parent_snapshot_id, :git_commit,
            :script_hash, :params_hash, :manifest_hash, :runtime_config_hash,
            :source_hash, :created_at, :created_by_trigger_id, :label, :legacy_run_id
        )""",
        snapshot,
    )
    conn.commit()
    conn.close()
    return snapshot


def get_snapshot(snapshot_id, root=None):
    root = resolve_root(root)
    conn = db(root)
    row = conn.execute(
        "select * from source_snapshots where snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"unknown snapshot_id: {snapshot_id}")
    return dict(row)


def list_snapshots(root=None):
    root = resolve_root(root)
    conn = db(root)
    rows = conn.execute(
        "select * from source_snapshots order by created_at desc, rowid desc"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def capture_workspace(root=None, *, parent_snapshot_id=None, label=None):
    root = resolve_root(root)
    commit, _ = git_commit_source("autoexp source snapshot", root)
    return _insert_snapshot(
        commit,
        root,
        root=root,
        parent_snapshot_id=parent_snapshot_id,
        label=label,
    )


def capture_source_tree(
    source_root,
    root=None,
    *,
    parent_snapshot_id=None,
    parent_commit=None,
    label=None,
):
    """Capture an already-materialized source tree without changing the workspace."""
    root = resolve_root(root)
    if parent_snapshot_id:
        parent = get_snapshot(parent_snapshot_id, root)
        parent_commit = parent["git_commit"]
    parent_commit = parent_commit or current_autoexp_commit(root)
    commit = commit_source_tree(
        source_root,
        parent_commit,
        "autoexp derived source snapshot",
        root,
    )
    return _insert_snapshot(
        commit,
        source_root,
        root=root,
        parent_snapshot_id=parent_snapshot_id,
        label=label,
    )


def materialize_snapshot(snapshot_id, destination, root=None):
    root = resolve_root(root)
    snapshot = get_snapshot(snapshot_id, root)
    materialize_commit(snapshot["git_commit"], destination, root)
    return snapshot


def diff_snapshots(snapshot_a, snapshot_b, root=None):
    root = resolve_root(root)
    left = get_snapshot(snapshot_a, root)
    right = get_snapshot(snapshot_b, root)
    return autoexp_git(
        ["diff", left["git_commit"], right["git_commit"], "--", *source_paths(root)],
        root=root,
        capture=True,
        check=False,
    )


def derive_snapshot(snapshot_id, changes, root=None, *, label=None):
    """Apply project-relative text replacements to a historical snapshot."""
    root = resolve_root(root)
    base = get_snapshot(snapshot_id, root)
    with tempfile.TemporaryDirectory(prefix="autoexp-snapshot-") as tmp:
        source_root = Path(tmp)
        materialize_commit(base["git_commit"], source_root, root)
        for raw_path, content in changes.items():
            rel = ensure_within_project(raw_path, "snapshot path must stay inside the project")
            path = source_root / rel
            if content is None:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        commit = commit_source_tree(
            source_root,
            base["git_commit"],
            f"autoexp snapshot derived from {snapshot_id}",
            root,
        )
        return _insert_snapshot(
            commit,
            source_root,
            root=root,
            parent_snapshot_id=snapshot_id,
            label=label,
        )


def migrate_legacy_run_snapshots(root=None):
    """Link legacy executions to snapshots and remove `edited` pseudo-runs."""
    root = resolve_root(root)
    conn = db(root)
    rows = conn.execute(
        "select * from runs where source_snapshot_id is null order by created_at, run_id"
    ).fetchall()
    conn.close()
    if not rows:
        return True

    try:
        head = current_autoexp_commit(root)
    except SystemExit:
        return False

    for raw in rows:
        run = dict(raw)
        conn = db(root)
        existing = conn.execute(
            "select * from source_snapshots where legacy_run_id = ?", (run["run_id"],)
        ).fetchone()
        conn.close()
        run_dir = root / (run.get("run_dir") or f"runs/{run['run_id']}")
        source_root = run_dir if (run_dir / "script").is_dir() else root
        if existing:
            snapshot = dict(existing)
        else:
            base_commit = run.get("stage_commit") or head
            commit = base_commit
            if source_root != root:
                commit = commit_source_tree(
                    source_root,
                    base_commit,
                    f"migrate legacy run {run['run_id']}",
                    root,
                )
            snapshot = _insert_snapshot(
                commit,
                source_root,
                root=root,
                created_at=run.get("created_at"),
                label=f"Imported legacy {run['status']} record",
                legacy_run_id=run["run_id"],
            )
        conn = db(root)
        if run["status"] == "edited":
            conn.execute("delete from runs where run_id = ?", (run["run_id"],))
        else:
            conn.execute(
                "update runs set source_snapshot_id = ? where run_id = ?",
                (snapshot["snapshot_id"], run["run_id"]),
            )
        conn.commit()
        conn.close()
    return True
