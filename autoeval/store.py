import json
import sqlite3
import subprocess
from pathlib import Path

from .workspace import die, project_root, source_paths


AUTOEVAL_GIT_DIR = ".autoeval/git"


def autoeval_git(args, root=None, capture=False, check=True):
    root = project_root() if root is None else Path(root)
    cmd = ["git", "--git-dir", str(root / AUTOEVAL_GIT_DIR), "--work-tree", str(root), *args]
    try:
        proc = subprocess.run(
            cmd, check=check, stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, text=True,
        )
    except FileNotFoundError:
        die("required command not found: git")
    return proc.stdout.strip() if capture else None


def require_autoeval_git_repo(root=None):
    root = project_root() if root is None else Path(root)
    if not (root / AUTOEVAL_GIT_DIR).is_dir():
        die(f"{root} is missing its Autoeval git repository")
    top = autoeval_git(["rev-parse", "--show-toplevel"], root=root, capture=True)
    if Path(top).resolve() != root.resolve():
        die("refusing to run git outside the autoeval project")


def db(root=None):
    root = project_root() if root is None else Path(root)
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


def insert_run(meta, root=None):
    from .runs import script_name

    row = {
        "run_dir": f"runs/{meta['run_id']}", "report_path": "", "output_hash": "",
        "script_name": meta.get("script_name") or script_name(meta["run_id"], root), **meta,
    }
    row["stage_status"] = json.dumps(row["stage_status"])
    conn = db(root)
    conn.execute(
        """insert into runs(
            run_id, run_dir, report_path, output_hash, capsule_hash, script_name,
            script_hash, script_env_hash, runtime_context_hash, stage_commit,
            status, stage_status, created_at
        ) values(
            :run_id, :run_dir, :report_path, :output_hash, :capsule_hash, :script_name,
            :script_hash, :script_env_hash, :runtime_context_hash, :stage_commit,
            :status, :stage_status, :created_at
        )""",
        row,
    )
    conn.commit()
    conn.close()


def update_run(meta, root=None):
    row = {**meta}
    row["stage_status"] = json.dumps(row["stage_status"])
    conn = db(root)
    conn.execute(
        """update runs set
            run_dir=:run_dir, report_path=:report_path, output_hash=:output_hash,
            capsule_hash=:capsule_hash, script_name=:script_name, script_hash=:script_hash,
            script_env_hash=:script_env_hash, runtime_context_hash=:runtime_context_hash,
            stage_commit=:stage_commit, status=:status, stage_status=:stage_status,
            created_at=:created_at where run_id=:run_id""",
        row,
    )
    conn.commit()
    conn.close()


def current_autoeval_commit(root=None):
    return autoeval_git(["rev-parse", "HEAD"], root=root, capture=True)


def git_status(paths, root=None):
    return autoeval_git(["status", "--porcelain", "--", *paths], root=root, capture=True)


def git_commit_source(message, root=None):
    paths = source_paths(root)
    autoeval_git(["add", *paths], root=root)
    if not autoeval_git(["diff", "--cached", "--name-only", "--", *paths], root=root, capture=True):
        return current_autoeval_commit(root), False
    autoeval_git(["commit", "-m", message, "--", *paths], root=root)
    return current_autoeval_commit(root), True
