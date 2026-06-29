import json
import sqlite3
import subprocess
from pathlib import Path

from .workspace import die, resolve_root, source_paths


AUTOEXP_GIT_DIR = ".autoexp/git"


# ======================================================================
#  Private Git: script/config snapshots separate from the user's repo
# ======================================================================

def autoexp_git(args, root=None, capture=False, check=True):
    """Run git against the project's private .autoexp/git store."""
    root = resolve_root(root)
    cmd = ["git", "--git-dir", str(root / AUTOEXP_GIT_DIR), "--work-tree", str(root), *args]
    try:
        proc = subprocess.run(
            cmd,
            check=check,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
        )
    except FileNotFoundError:
        die("required command not found: git")
    return proc.stdout.strip() if capture else None


def require_autoexp_git_repo(root=None):
    """Fail unless the private git store exists and is rooted at the project."""
    root = resolve_root(root)
    if not (root / AUTOEXP_GIT_DIR).is_dir():
        die(f"{root} is missing its Autoexp git repository")
    top = autoexp_git(["rev-parse", "--show-toplevel"], root=root, capture=True)
    if Path(top).resolve() != root.resolve():
        die("refusing to run git outside the autoexp project")


def current_autoexp_commit(root=None):
    return autoexp_git(["rev-parse", "HEAD"], root=root, capture=True)


def git_status(paths, root=None):
    return autoexp_git(["status", "--porcelain", "--", *paths], root=root, capture=True)


def git_commit_source(message, root=None):
    """Stage and commit the source set; return (commit, changed?)."""
    paths = source_paths(root)
    autoexp_git(["add", *paths], root=root)
    staged = autoexp_git(["diff", "--cached", "--name-only", "--", *paths], root=root, capture=True)
    if not staged:
        return current_autoexp_commit(root), False
    autoexp_git(["commit", "-m", message, "--", *paths], root=root)
    return current_autoexp_commit(root), True


# ======================================================================
#  Run index (index.sqlite)
# ======================================================================

def db(root=None):
    root = resolve_root(root)
    conn = sqlite3.connect(root / "index.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(root=None):
    conn = db(root)
    conn.executescript(
        """
        create table if not exists runs(
            run_id text primary key, run_dir text not null, report_path text not null,
            output_hash text not null, capsule_hash text not null, script_name text not null,
            script_hash text not null, script_env_hash text not null,
            runtime_context_hash text not null, stage_commit text not null,
            status text not null, stage_status text not null, created_at text not null
        );
        create index if not exists idx_runs_capsule on runs(capsule_hash, status);
        """
    )
    conn.commit()
    conn.close()


RUN_COLUMNS = (
    "run_id", "run_dir", "report_path", "output_hash", "capsule_hash", "script_name",
    "script_hash", "script_env_hash", "runtime_context_hash", "stage_commit",
    "status", "stage_status", "created_at",
)


def _row_for_db(meta):
    """Copy meta and JSON-encode the columns the table stores as text."""
    row = {**meta}
    row["stage_status"] = json.dumps(row["stage_status"])
    return row


def insert_run(meta, root=None):
    from .runs import script_name

    row = {
        "run_dir": f"runs/{meta['run_id']}",
        "report_path": "",
        "output_hash": "",
        "script_name": meta.get("script_name") or script_name(meta["run_id"], root),
        **meta,
    }
    row = _row_for_db(row)
    placeholders = ", ".join(f":{col}" for col in RUN_COLUMNS)
    conn = db(root)
    conn.execute(
        f"insert into runs({', '.join(RUN_COLUMNS)}) values({placeholders})",
        row,
    )
    conn.commit()
    conn.close()


def update_run(meta, root=None):
    row = _row_for_db(meta)
    assignments = ", ".join(f"{col}=:{col}" for col in RUN_COLUMNS if col != "run_id")
    conn = db(root)
    conn.execute(f"update runs set {assignments} where run_id=:run_id", row)
    conn.commit()
    conn.close()
