"""Private-Git primitives for immutable source snapshots."""

import os
import subprocess
import tempfile
from pathlib import Path

from .store import AUTOEXP_GIT_DIR, autoexp_git
from .workspace import resolve_root, source_paths


def _git(root, args, *, env=None):
    root = resolve_root(root)
    command = ["git", "--git-dir", str(root / AUTOEXP_GIT_DIR), *args]
    proc = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


def _temporary_index(root):
    root = resolve_root(root)
    fd, name = tempfile.mkstemp(prefix="snapshot-index-", dir=root / ".autoexp")
    os.close(fd)
    Path(name).unlink()
    return Path(name)


def materialize_commit(commit, destination, root=None):
    """Materialize a private-Git commit without changing its checkout or index."""
    root = resolve_root(root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"snapshot destination is not empty: {destination}")
    index = _temporary_index(root)
    env = os.environ | {"GIT_INDEX_FILE": str(index)}
    try:
        _git(root, ["read-tree", commit], env=env)
        prefix = f"{destination.resolve()}{os.sep}"
        _git(root, ["checkout-index", "--all", "--force", f"--prefix={prefix}"], env=env)
    finally:
        index.unlink(missing_ok=True)


def commit_source_tree(source_root, parent_commit, message, root=None):
    """Commit a complete source tree without touching the active workspace."""
    root = resolve_root(root)
    source_root = Path(source_root)
    index = _temporary_index(root)
    env = os.environ | {"GIT_INDEX_FILE": str(index)}
    try:
        _git(root, ["read-tree", parent_commit], env=env)
        paths = source_paths(source_root)
        _git(root, [f"--work-tree={source_root}", "add", "-A", "--", *paths], env=env)
        tree = _git(root, ["write-tree"], env=env)
        return _git(root, ["commit-tree", tree, "-p", parent_commit, "-m", message], env=env)
    finally:
        index.unlink(missing_ok=True)


def preserve_snapshot_ref(snapshot_id, commit, root=None):
    autoexp_git(["update-ref", f"refs/autoexp/snapshots/{snapshot_id}", commit], root=root)
