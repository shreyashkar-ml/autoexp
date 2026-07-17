"""Private-Git primitives for immutable source snapshots."""

import os
import subprocess
import tempfile
from pathlib import Path

from .store import autoexp_git, private_git_dir, require_autoexp_git_repo
from .workspace import resolve_root, source_paths


def _git(root, args, *, env=None):
    root = resolve_root(root)
    command = ["git", "--git-dir", str(private_git_dir(root)), *args]
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
    fd, name = tempfile.mkstemp(prefix="snapshot-index-", dir=private_git_dir(root).parent)
    os.close(fd)
    Path(name).unlink()
    return Path(name)


def materialize_commit(commit, destination, root=None):
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError(f"snapshot destination must not be a symlink: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"snapshot destination is not empty: {destination}")
    index = _temporary_index(root)
    env = os.environ | {"GIT_INDEX_FILE": str(index)}
    try:
        _git(root, ["read-tree", commit], env=env)
        _git(root, [f"--work-tree={destination}", "checkout-index", "--all", "--force", f"--prefix={destination.resolve()}{os.sep}"], env=env)
    finally:
        index.unlink(missing_ok=True)


def commit_source_tree(source_root, parent_commit, message, root=None):
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    source_root = Path(source_root)
    index = _temporary_index(root)
    env = os.environ | {
        "GIT_INDEX_FILE": str(index),
        "GIT_AUTHOR_NAME": "Autoexp",
        "GIT_AUTHOR_EMAIL": "autoexp@local",
        "GIT_COMMITTER_NAME": "Autoexp",
        "GIT_COMMITTER_EMAIL": "autoexp@local",
    }
    try:
        _git(root, ["read-tree", parent_commit], env=env)
        paths = source_paths(source_root)
        _git(root, [f"--work-tree={source_root}", "add", "-f", "-A", "--", *paths], env=env)
        tree = _git(root, ["write-tree"], env=env)
        return _git(root, ["commit-tree", tree, "-p", parent_commit, "-m", message], env=env)
    finally:
        index.unlink(missing_ok=True)


def preserve_snapshot_ref(snapshot_id, commit, root=None):
    require_autoexp_git_repo(root)
    autoexp_git(["update-ref", f"refs/autoexp/snapshots/{snapshot_id}", commit], root=root)
