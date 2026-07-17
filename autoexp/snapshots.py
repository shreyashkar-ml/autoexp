"""Immutable source snapshot capture, derivation, and materialization."""

import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from .git_store import commit_source_tree, materialize_commit, preserve_snapshot_ref
from .store import autoexp_git, current_autoexp_commit, db, require_autoexp_git_repo
from .workspace import (
    PARAMS_FILE,
    PROJECT_CONFIG,
    STAGE_MANIFEST,
    ensure_within_project,
    experiment_id,
    materialize_workspace,
    now,
    project_id,
    read_json,
    resolve_root,
    source_paths,
)


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _hash_file(path):
    if path.is_symlink():
        raise ValueError(f"source contains unsupported symlink: {path}")
    return _hash_bytes(path.read_bytes()) if path.is_file() else _hash_bytes(b"")


def _hash_declared_source(source_root, config, *, include_types=True):
    digest = hashlib.sha256()
    for item in sorted(config.get("files", []), key=lambda value: value.get("path", "")):
        if item.get("role") in {"secret-source", "generated-output"}:
            continue
        rel = item.get("path", "")
        path = source_root / rel
        if path.is_symlink():
            raise ValueError(f"source contains unsupported symlink: {rel}")
        digest.update(rel.encode())
        digest.update(b"\0")
        if include_types and path.is_file():
            digest.update(b"file\0")
        elif include_types and path.exists():
            digest.update(b"other\0")
        elif include_types:
            digest.update(b"missing\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _legacy_snapshot_hashes(source_root, config):
    """Reproduce the source identity written by repo-local Autoexp 0.2."""
    digest = hashlib.sha256()
    script_dir = source_root / "experiment"
    if script_dir.is_symlink():
        raise ValueError("script directory must not be a symlink")
    for path in sorted(script_dir.rglob("*")) if script_dir.is_dir() else ():
        if path.is_symlink():
            raise ValueError(f"source contains unsupported symlink: {path.relative_to(script_dir)}")
        if path.is_file():
            digest.update(path.relative_to(script_dir).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    runtime_config = {
        key: config.get(key)
        for key in ("runner", "sandbox", "runtime")
        if key in config
    }
    hashes = {
        "script_hash": digest.hexdigest(),
        "params_hash": _hash_file(source_root / PARAMS_FILE),
        "manifest_hash": _hash_file(source_root / STAGE_MANIFEST),
        "runtime_config_hash": _hash_bytes(
            json.dumps(runtime_config, sort_keys=True, separators=(",", ":")).encode()
        ),
    }
    hashes["source_hash"] = _hash_bytes(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    )
    return hashes


def _safe_change_path(source_root, rel):
    source_root = Path(source_root).resolve()
    path = source_root / rel
    cursor = source_root
    for part in Path(rel).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"snapshot path must not contain symlinks: {rel}")
    if not path.resolve(strict=False).is_relative_to(source_root):
        raise ValueError("snapshot path must stay inside its source tree")
    return path


def snapshot_hashes(source_root, *, include_types=True):
    source_root = Path(source_root)
    config = read_json(source_root / PROJECT_CONFIG)
    if "files" not in config and isinstance(config.get("source"), dict):
        return _legacy_snapshot_hashes(source_root, config)
    runtime_config = {
        key: config.get(key)
        for key in ("runner", "sandbox", "runtime")
        if key in config
    }
    hashes = {
        "script_hash": _hash_declared_source(source_root, config, include_types=include_types),
        "params_hash": _hash_file(source_root / PARAMS_FILE),
        "manifest_hash": _hash_file(source_root / STAGE_MANIFEST),
        "runtime_config_hash": _hash_bytes(
            json.dumps(runtime_config, sort_keys=True, separators=(",", ":")).encode()
        ),
    }
    hashes["source_hash"] = _hash_bytes(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    )
    return hashes


def snapshot_matches(snapshot, source_root):
    # ponytail: legacy snapshots lack type markers; remove fallback with 0.2 import support.
    return any(
        snapshot_hashes(source_root, include_types=include_types)["source_hash"]
        == snapshot["source_hash"]
        for include_types in (True, False)
    )


def _insert_snapshot(
    commit,
    source_root,
    *,
    root,
    hashes=None,
    parent_snapshot_id=None,
    created_at=None,
    created_by_trigger_id=None,
    label=None,
    legacy_run_id=None,
):
    hashes = hashes or snapshot_hashes(source_root)
    snapshot_id = f"snap_{hashes['source_hash'][:8]}_{uuid.uuid4().hex[:6]}"
    snapshot = {
        "snapshot_id": snapshot_id,
        "repo_id": project_id(root),
        "experiment_id": experiment_id(root),
        "parent_snapshot_id": parent_snapshot_id,
        "git_commit": commit,
        **hashes,
        "created_at": created_at or now(),
        "created_by_trigger_id": created_by_trigger_id,
        "label": label,
        "legacy_run_id": legacy_run_id,
    }
    preserve_snapshot_ref(snapshot_id, commit, root)
    conn = db()
    conn.execute(
        """insert into source_snapshots(
             snapshot_id, repo_id, experiment_id, parent_snapshot_id, git_commit,
             script_hash, params_hash, manifest_hash, runtime_config_hash,
             source_hash, created_at, created_by_trigger_id, label, legacy_run_id
           ) values(
             :snapshot_id, :repo_id, :experiment_id, :parent_snapshot_id, :git_commit,
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
    conn = db()
    row = conn.execute(
        "select * from source_snapshots where snapshot_id = ? and experiment_id = ?",
        (snapshot_id, experiment_id(root)),
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"unknown snapshot_id: {snapshot_id}")
    return dict(row)


def list_snapshots(root=None):
    root = resolve_root(root)
    conn = db()
    rows = conn.execute(
        """select * from source_snapshots where experiment_id = ?
           order by created_at desc, rowid desc""",
        (experiment_id(root),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def capture_workspace(
    root=None,
    *,
    parent_snapshot_id=None,
    created_by_trigger_id=None,
    label=None,
):
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    with tempfile.TemporaryDirectory(prefix="autoexp-live-source-") as tmp:
        source_root = materialize_workspace(root, tmp)
        return capture_source_tree(
            source_root,
            root,
            parent_snapshot_id=parent_snapshot_id,
            created_by_trigger_id=created_by_trigger_id,
            label=label,
        )


def capture_source_tree(
    source_root,
    root=None,
    *,
    parent_snapshot_id=None,
    parent_commit=None,
    created_by_trigger_id=None,
    label=None,
):
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    hashes = snapshot_hashes(source_root)
    if parent_snapshot_id:
        parent_commit = get_snapshot(parent_snapshot_id, root)["git_commit"]
    parent_commit = parent_commit or current_autoexp_commit(root)
    commit = commit_source_tree(source_root, parent_commit, "autoexp source snapshot", root)
    return _insert_snapshot(
        commit,
        source_root,
        root=root,
        hashes=hashes,
        parent_snapshot_id=parent_snapshot_id,
        created_by_trigger_id=created_by_trigger_id,
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
        ["diff", left["git_commit"], right["git_commit"], "--"],
        root=root,
        capture=True,
        check=False,
    )


def derive_snapshot(
    snapshot_id,
    changes,
    root=None,
    *,
    created_by_trigger_id=None,
    label=None,
):
    root = resolve_root(root)
    base = get_snapshot(snapshot_id, root)
    with tempfile.TemporaryDirectory(prefix="autoexp-snapshot-") as tmp:
        source_root = Path(tmp)
        materialize_commit(base["git_commit"], source_root, root)
        allowed = set(source_paths(source_root))
        for raw_path, content in changes.items():
            rel = ensure_within_project(raw_path, "snapshot path must stay inside its source tree")
            if rel.as_posix() not in allowed:
                raise ValueError(f"path is not declared by this experiment: {rel}")
            path = _safe_change_path(source_root, rel)
            if content is None:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        return capture_source_tree(
            source_root,
            root,
            parent_snapshot_id=snapshot_id,
            created_by_trigger_id=created_by_trigger_id,
            label=label,
        )
