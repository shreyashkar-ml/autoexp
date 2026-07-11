import json
import sqlite3
import subprocess
from pathlib import Path

from .workspace import die, resolve_root, source_paths


AUTOEXP_GIT_DIR = ".autoexp/git"


IMMUTABILITY_TRIGGERS = """
create trigger if not exists runs_terminal_immutable
before update on runs
when old.status in ('success', 'failed', 'canceled') and (
    new.run_id is not old.run_id or
    new.run_dir is not old.run_dir or
    new.output_hash is not old.output_hash or
    new.capsule_hash is not old.capsule_hash or
    new.script_name is not old.script_name or
    new.script_hash is not old.script_hash or
    new.script_env_hash is not old.script_env_hash or
    new.runtime_context_hash is not old.runtime_context_hash or
    new.stage_commit is not old.stage_commit or
    new.status is not old.status or
    new.stage_status is not old.stage_status or
    new.created_at is not old.created_at or
    new.source_snapshot_id is not old.source_snapshot_id or
    new.parent_run_id is not old.parent_run_id or
    new.trigger_id is not old.trigger_id or
    new.exit_code is not old.exit_code or
    new.runner is not old.runner or
    new.runner_identity is not old.runner_identity or
    new.started_at is not old.started_at or
    new.ended_at is not old.ended_at or
    new.duration_ms is not old.duration_ms or
    new.failure_kind is not old.failure_kind or
    new.failure_message is not old.failure_message or
    new.reproduces_run_id is not old.reproduces_run_id
)
begin
    select raise(abort, 'terminal run is immutable');
end;

create trigger if not exists runs_terminal_no_delete
before delete on runs
when old.status in ('success', 'failed', 'canceled')
begin
    select raise(abort, 'terminal run is immutable');
end;

create trigger if not exists artifacts_terminal_no_insert
before insert on artifacts
when new.category in ('output', 'log') and exists(
    select 1 from runs where run_id = new.run_id
    and status in ('success', 'failed', 'canceled')
)
begin
    select raise(abort, 'terminal execution artifacts are immutable');
end;

create trigger if not exists artifacts_immutable_no_update
before update on artifacts
when old.category = 'report' or
    (old.category in ('output', 'log') and exists(
        select 1 from runs where run_id = old.run_id
        and status in ('success', 'failed', 'canceled')
    ))
begin
    select raise(abort, 'artifact is immutable');
end;

create trigger if not exists artifacts_immutable_no_delete
before delete on artifacts
when old.category = 'report' or
    (old.category in ('output', 'log') and exists(
        select 1 from runs where run_id = old.run_id
        and status in ('success', 'failed', 'canceled')
    ))
begin
    select raise(abort, 'artifact is immutable');
end;

create trigger if not exists external_inputs_terminal_no_insert
before insert on run_external_inputs
when exists(
    select 1 from runs where run_id = new.run_id
    and status in ('success', 'failed', 'canceled')
)
begin
    select raise(abort, 'terminal run external inputs are immutable');
end;

create trigger if not exists external_inputs_terminal_no_update
before update on run_external_inputs
when exists(
    select 1 from runs where run_id = old.run_id
    and status in ('success', 'failed', 'canceled')
)
begin
    select raise(abort, 'terminal run external inputs are immutable');
end;

create trigger if not exists external_inputs_terminal_no_delete
before delete on run_external_inputs
when exists(
    select 1 from runs where run_id = old.run_id
    and status in ('success', 'failed', 'canceled')
)
begin
    select raise(abort, 'terminal run external inputs are immutable');
end;
"""


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
    git_dir = root / AUTOEXP_GIT_DIR
    if (
        git_dir.is_symlink()
        or not git_dir.resolve().is_relative_to(root.resolve())
        or not git_dir.is_dir()
    ):
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
    conn.execute("pragma foreign_keys = on")
    return conn


def init_db(root=None):
    root = resolve_root(root)
    conn = db(root)
    has_runs = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'runs'"
    ).fetchone()
    conn.execute(
        """create table if not exists schema_metadata(
            schema_version integer not null,
            legacy_snapshot_migration_complete integer not null default 0
        )"""
    )
    version = conn.execute("select schema_version from schema_metadata").fetchone()
    if version is None:
        conn.execute(
            "insert into schema_metadata(schema_version) values (?)",
            (1 if has_runs else 0,),
        )
        version = (1 if has_runs else 0,)

    if version[0] < 1:
        conn.executescript(
            """
        begin;
        create table if not exists runs(
            run_id text primary key, run_dir text not null, report_path text not null,
            output_hash text not null, capsule_hash text not null, script_name text not null,
            script_hash text not null, script_env_hash text not null,
            runtime_context_hash text not null, stage_commit text not null,
            status text not null, stage_status text not null, created_at text not null
        );
        create index if not exists idx_runs_capsule on runs(capsule_hash, status);
        update schema_metadata set schema_version = 1;
        commit;
            """
        )

    if version[0] < 2:
        conn.executescript(
            """
            begin;
            create table source_snapshots(
                snapshot_id text primary key,
                project_id text not null,
                parent_snapshot_id text references source_snapshots(snapshot_id),
                git_commit text not null,
                script_hash text not null,
                params_hash text not null,
                manifest_hash text not null,
                runtime_config_hash text not null,
                source_hash text not null,
                created_at text not null,
                created_by_trigger_id text,
                label text,
                legacy_run_id text unique
            );
            create index idx_source_snapshots_hash on source_snapshots(source_hash);
            alter table runs add column source_snapshot_id text references source_snapshots(snapshot_id);
            update schema_metadata
            set schema_version = 2, legacy_snapshot_migration_complete = 0;
            commit;
            """
        )

    if version[0] < 3:
        conn.executescript(
            """
            begin;
            create table triggers(
                trigger_id text primary key,
                kind text not null,
                actor_name text,
                session_id text,
                request_id text,
                metadata text not null default '{}',
                created_at text not null
            );
            create index idx_triggers_request on triggers(request_id);

            create table artifacts(
                artifact_id text primary key,
                run_id text not null references runs(run_id) on delete restrict,
                category text not null check(category in ('output', 'log', 'report')),
                path text not null,
                media_type text not null,
                content_hash text not null,
                size_bytes integer not null check(size_bytes >= 0),
                created_at text not null,
                metadata text not null default '{}',
                unique(run_id, path)
            );
            create index idx_artifacts_run_category on artifacts(run_id, category);

            create table run_external_inputs(
                run_id text not null references runs(run_id) on delete restrict,
                name text not null,
                kind text not null,
                present integer not null check(present in (0, 1)),
                fingerprint text,
                version text,
                reproducibility_state text not null
                    check(reproducibility_state in ('pinned', 'unpinned', 'redacted')),
                metadata text not null default '{}',
                primary key(run_id, name)
            );

            alter table runs add column parent_run_id text references runs(run_id);
            alter table runs add column trigger_id text references triggers(trigger_id);
            alter table runs add column exit_code integer;
            alter table runs add column runner text;
            alter table runs add column runner_identity text;
            alter table runs add column started_at text;
            alter table runs add column ended_at text;
            alter table runs add column duration_ms integer;
            alter table runs add column failure_kind text;
            alter table runs add column failure_message text;
            alter table runs add column reproduces_run_id text references runs(run_id);
            alter table schema_metadata add column standard_migration_complete integer not null default 0;
            create index idx_runs_parent on runs(parent_run_id);
            create index idx_runs_trigger on runs(trigger_id);
            create index idx_runs_reproduces on runs(reproduces_run_id);

            create trigger runs_validate_insert
            before insert on runs
            when new.status not in ('queued', 'running', 'success', 'failed', 'canceled')
            begin
                select raise(abort, 'invalid run status');
            end;

            create trigger runs_validate_transition
            before update of status on runs
            when new.status != old.status and not (
                (old.status = 'queued' and new.status = 'running') or
                (old.status = 'running' and new.status in ('success', 'failed', 'canceled'))
            )
            begin
                select raise(abort, 'invalid run status transition');
            end;

            update schema_metadata set schema_version = 3;
            commit;
            """
        )
    conn.commit()
    migration_complete = conn.execute(
        "select legacy_snapshot_migration_complete from schema_metadata"
    ).fetchone()[0]
    conn.close()

    if not migration_complete:
        from .snapshots import migrate_legacy_run_snapshots

        if migrate_legacy_run_snapshots(root):
            conn = db(root)
            conn.execute(
                "update schema_metadata set legacy_snapshot_migration_complete = 1"
            )
            conn.commit()
            conn.close()

    conn = db(root)
    standard_migration_complete = conn.execute(
        "select standard_migration_complete from schema_metadata"
    ).fetchone()[0]
    conn.close()
    if not standard_migration_complete:
        from .artifacts import migrate_existing_artifacts
        from .provenance import migrate_legacy_provenance

        migrate_legacy_provenance(root)
        migrate_existing_artifacts(root)
        conn = db(root)
        conn.execute("update schema_metadata set standard_migration_complete = 1")
        conn.commit()
        conn.close()

    conn = db(root)
    conn.executescript(IMMUTABILITY_TRIGGERS)
    conn.close()


RUN_COLUMNS = (
    "run_id", "run_dir", "report_path", "output_hash", "capsule_hash", "script_name",
    "script_hash", "script_env_hash", "runtime_context_hash", "stage_commit",
    "status", "stage_status", "created_at", "source_snapshot_id", "parent_run_id",
    "trigger_id", "exit_code", "runner", "runner_identity", "started_at", "ended_at",
    "duration_ms", "failure_kind", "failure_message", "reproduces_run_id",
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
        "source_snapshot_id": None,
        "parent_run_id": None,
        "trigger_id": None,
        "exit_code": None,
        "runner": None,
        "runner_identity": None,
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "failure_kind": None,
        "failure_message": None,
        "reproduces_run_id": None,
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
    conn = db(root)
    existing = conn.execute("select * from runs where run_id = ?", (meta["run_id"],)).fetchone()
    if existing is None:
        conn.close()
        raise ValueError(f"unknown run_id: {meta['run_id']}")
    current = dict(existing)
    current["stage_status"] = json.loads(current["stage_status"])
    row = _row_for_db(current | meta)
    assignments = ", ".join(f"{col}=:{col}" for col in RUN_COLUMNS if col != "run_id")
    conn.execute(f"update runs set {assignments} where run_id=:run_id", row)
    conn.commit()
    conn.close()
