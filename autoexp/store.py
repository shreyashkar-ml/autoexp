"""One global SQLite ledger and one private Git snapshot store per worktree."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from .workspace import (
    die,
    experiment_id,
    project_id,
    repo_data_dir,
    resolve_root,
    user_data_dir,
)


SCHEMA = """
pragma foreign_keys = on;
create table if not exists schema_metadata(schema_version integer not null);
insert into schema_metadata(schema_version)
select 1 where not exists(select 1 from schema_metadata);

create table if not exists repositories(
  repo_id text primary key, title text not null, path text not null unique,
  created_at text not null, last_opened_at text not null
);
create table if not exists experiments(
  experiment_id text primary key,
  repo_id text not null references repositories(repo_id),
  title text not null, objective text not null,
  kind text not null check(kind in ('standard', 'autoresearch')),
  status text not null,
  runner text not null check(runner in ('local', 'docker')),
  stage text not null check(json_valid(stage)),
  config text not null check(json_valid(config)),
  params text not null check(json_valid(params)),
  params_schema text not null check(json_valid(params_schema)),
  report_guidance text not null,
  data_path text not null unique,
  created_at text not null, updated_at text not null
);
create index if not exists idx_experiments_repo on experiments(repo_id, updated_at desc);
create table if not exists manifest_files(
  experiment_id text not null references experiments(experiment_id),
  path text not null, role text not null, description text not null default '',
  content_hash text, available integer not null check(available in (0, 1)),
  secret_keys text not null default '[]' check(json_valid(secret_keys)),
  updated_at text not null, primary key(experiment_id, path)
);
create table if not exists triggers(
  trigger_id text primary key,
  experiment_id text not null references experiments(experiment_id),
  kind text not null, actor_name text, session_id text, request_id text,
  metadata text not null default '{}' check(json_valid(metadata)), created_at text not null
);
create table if not exists source_snapshots(
  snapshot_id text primary key,
  repo_id text not null references repositories(repo_id),
  experiment_id text not null references experiments(experiment_id),
  parent_snapshot_id text references source_snapshots(snapshot_id),
  git_commit text not null, script_hash text not null, params_hash text not null,
  manifest_hash text not null, runtime_config_hash text not null, source_hash text not null,
  created_at text not null, created_by_trigger_id text references triggers(trigger_id),
  label text, legacy_run_id text unique
);
create index if not exists idx_snapshots_experiment on source_snapshots(experiment_id, created_at desc);
create table if not exists runs(
  run_id text primary key,
  experiment_id text not null references experiments(experiment_id),
  run_dir text not null, report_path text not null, output_hash text not null,
  capsule_hash text not null, script_name text not null, script_hash text not null,
  script_env_hash text not null, runtime_context_hash text not null, stage_commit text not null,
  status text not null check(status in ('queued', 'running', 'success', 'failed', 'canceled')),
  stage_status text not null check(json_valid(stage_status)), created_at text not null,
  source_snapshot_id text references source_snapshots(snapshot_id),
  parent_run_id text references runs(run_id), trigger_id text references triggers(trigger_id),
  exit_code integer, runner text, runner_identity text, started_at text, ended_at text,
  duration_ms integer, failure_kind text, failure_message text,
  reproduces_run_id text references runs(run_id)
);
create index if not exists idx_runs_experiment on runs(experiment_id, created_at desc);
create index if not exists idx_runs_capsule on runs(experiment_id, capsule_hash, status);
create table if not exists artifacts(
  artifact_id text primary key, run_id text not null references runs(run_id) on delete restrict,
  category text not null check(category in ('output', 'log', 'report')),
  path text not null, media_type text not null, content_hash text not null,
  size_bytes integer not null check(size_bytes >= 0), created_at text not null,
  metadata text not null default '{}' check(json_valid(metadata)), unique(run_id, path)
);
create table if not exists run_external_inputs(
  run_id text not null references runs(run_id) on delete restrict,
  name text not null, kind text not null, present integer not null check(present in (0, 1)),
  fingerprint text, version text,
  reproducibility_state text not null check(reproducibility_state in ('pinned', 'unpinned', 'redacted')),
  metadata text not null default '{}' check(json_valid(metadata)), primary key(run_id, name)
);
create table if not exists milestones(
  milestone_id text primary key,
  experiment_id text not null references experiments(experiment_id),
  target_kind text not null check(target_kind in ('run', 'attempt')), target_id text not null,
  title text not null, significance text not null, actor_name text, created_at text not null,
  unique(experiment_id, target_kind, target_id)
);
create table if not exists documents(
  document_id text primary key,
  experiment_id text not null references experiments(experiment_id),
  run_id text references runs(run_id), kind text not null check(kind in ('report', 'insight')),
  title text not null, path text not null, content_hash text not null,
  size_bytes integer not null, created_at text not null, unique(experiment_id, path)
);
create table if not exists review_sessions(
  session_id text primary key,
  experiment_id text not null references experiments(experiment_id),
  token_hash text not null unique,
  status text not null check(status in ('waiting', 'completed', 'expired')),
  expires_at integer not null, notes text not null default '[]' check(json_valid(notes)),
  created_at text not null, completed_at text
);
create table if not exists research_contracts(
  contract_id text primary key,
  experiment_id text not null references experiments(experiment_id),
  parent_contract_id text references research_contracts(contract_id),
  status text not null check(status in ('active', 'superseded')), contract_hash text not null,
  metric text not null, direction text not null check(direction in ('min', 'max')),
  baseline_score real, best_score real, best_snapshot_id text references source_snapshots(snapshot_id),
  evaluator_path text not null, evaluator_hash text not null, program_path text not null,
  subject_path text not null, budget_sec integer not null check(budget_sec > 0),
  metric_source text not null check(json_valid(metric_source)),
  agent_command text not null check(json_valid(agent_command)), created_at text not null, ended_at text
);
create unique index if not exists idx_research_contract_active
on research_contracts(experiment_id) where status = 'active';
create table if not exists research_sessions(
  session_id text primary key, contract_id text not null references research_contracts(contract_id),
  status text not null check(status in ('running', 'stopped', 'completed', 'failed', 'interrupted')),
  phase text not null check(phase in ('idle', 'propose', 'train', 'score')),
  attempt_id text, pid integer, log_path text not null, started_at text not null,
  ended_at text, failure_message text
);
create unique index if not exists idx_research_session_running
on research_sessions(contract_id) where status = 'running';
create table if not exists research_attempts(
  contract_id text not null references research_contracts(contract_id), attempt_id text not null,
  sequence integer not null check(sequence > 0), session_id text references research_sessions(session_id),
  status text not null check(status in ('running', 'scored', 'failed')), hypothesis text not null,
  base_snapshot_id text references source_snapshots(snapshot_id),
  candidate_snapshot_id text references source_snapshots(snapshot_id), run_id text references runs(run_id),
  score real, verdict text check(verdict in ('kept', 'reverted')), best_score_before real,
  improvement real, created_at text, ended_at text, failure_message text,
  metadata text not null default '{}' check(json_valid(metadata)),
  primary key(contract_id, attempt_id), unique(contract_id, sequence)
);
create unique index if not exists idx_research_attempt_running
on research_attempts(contract_id) where status = 'running';
create table if not exists imports(
  import_id text primary key, source_path text not null unique,
  experiment_id text not null references experiments(experiment_id),
  summary text not null check(json_valid(summary)), created_at text not null
);

create trigger if not exists runs_validate_transition before update of status on runs
when new.status != old.status and not (
  (old.status = 'queued' and new.status = 'running') or
  (old.status = 'running' and new.status in ('success', 'failed', 'canceled'))
) begin select raise(abort, 'invalid run status transition'); end;
create trigger if not exists runs_terminal_no_delete before delete on runs
when old.status in ('success', 'failed', 'canceled')
begin select raise(abort, 'terminal run is immutable'); end;
create trigger if not exists runs_terminal_immutable before update on runs
when old.status in ('success', 'failed', 'canceled')
begin select raise(abort, 'terminal run is immutable'); end;
create trigger if not exists artifacts_immutable_no_update before update on artifacts
when old.category = 'report' or exists(
  select 1 from runs where run_id = old.run_id and status in ('success', 'failed', 'canceled')
) begin select raise(abort, 'artifact is immutable'); end;
create trigger if not exists artifacts_terminal_no_insert before insert on artifacts
when new.category in ('output', 'log') and exists(
  select 1 from runs where run_id = new.run_id and status in ('success', 'failed', 'canceled')
) begin select raise(abort, 'terminal run evidence is immutable'); end;
create trigger if not exists snapshots_immutable_no_update before update on source_snapshots
begin select raise(abort, 'source snapshot is immutable'); end;
create trigger if not exists snapshots_immutable_no_delete before delete on source_snapshots
begin select raise(abort, 'source snapshot is immutable'); end;
create trigger if not exists triggers_immutable_no_update before update on triggers
begin select raise(abort, 'trigger is immutable'); end;
create trigger if not exists triggers_immutable_no_delete before delete on triggers
begin select raise(abort, 'trigger is immutable'); end;
create trigger if not exists documents_immutable_no_update before update on documents
begin select raise(abort, 'document is immutable'); end;
create trigger if not exists documents_immutable_no_delete before delete on documents
begin select raise(abort, 'document is immutable'); end;
create trigger if not exists milestones_immutable_no_update before update on milestones
begin select raise(abort, 'milestone is immutable'); end;
create trigger if not exists milestones_immutable_no_delete before delete on milestones
begin select raise(abort, 'milestone is immutable'); end;
create trigger if not exists attempts_terminal_no_update before update on research_attempts
when old.status in ('scored', 'failed')
begin select raise(abort, 'research attempt is immutable'); end;
create trigger if not exists attempts_terminal_no_delete before delete on research_attempts
when old.status in ('scored', 'failed')
begin select raise(abort, 'research attempt is immutable'); end;
create trigger if not exists inputs_terminal_no_update before update on run_external_inputs
when exists(select 1 from runs where run_id = old.run_id and status in ('success', 'failed', 'canceled'))
begin select raise(abort, 'run input evidence is immutable'); end;
create trigger if not exists inputs_terminal_no_insert before insert on run_external_inputs
when exists(select 1 from runs where run_id = new.run_id and status in ('success', 'failed', 'canceled'))
begin select raise(abort, 'terminal run evidence is immutable'); end;
create trigger if not exists inputs_terminal_no_delete before delete on run_external_inputs
when exists(select 1 from runs where run_id = old.run_id and status in ('success', 'failed', 'canceled'))
begin select raise(abort, 'run input evidence is immutable'); end;

create trigger if not exists artifacts_immutable_no_delete before delete on artifacts
when old.category = 'report' or exists(
  select 1 from runs where run_id = old.run_id and status in ('success', 'failed', 'canceled')
) begin select raise(abort, 'artifact is immutable'); end;
"""


def db(root=None):
    path = user_data_dir() / "state.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.executescript(SCHEMA)
    return conn


def private_git_dir(root):
    return repo_data_dir(project_id(root)) / "repository"


def _git_env():
    return os.environ | {
        "GIT_AUTHOR_NAME": "Autoexp",
        "GIT_AUTHOR_EMAIL": "autoexp@local",
        "GIT_COMMITTER_NAME": "Autoexp",
        "GIT_COMMITTER_EMAIL": "autoexp@local",
    }


def autoexp_git(args, root=None, capture=False, check=True, *, input_text=None, env=None):
    git_dir = private_git_dir(resolve_root(root))
    cmd = ["git", "--git-dir", str(git_dir), *args]
    try:
        proc = subprocess.run(
            cmd,
            check=check,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_git_env() | (env or {}),
        )
    except FileNotFoundError:
        die("required command not found: git")
    return proc.stdout.strip() if capture else None


def require_autoexp_git_repo(root=None):
    root = resolve_root(root)
    git_dir = private_git_dir(root)
    if git_dir.is_dir():
        return git_dir
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", str(git_dir)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    tree = autoexp_git(["mktree"], root, capture=True, input_text="")
    commit = autoexp_git(["commit-tree", tree, "-m", "autoexp snapshot root"], root, capture=True)
    autoexp_git(["update-ref", "refs/heads/main", commit], root)
    return git_dir


def current_autoexp_commit(root=None):
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    conn = db()
    row = conn.execute(
        """select git_commit from source_snapshots
           where experiment_id = ? order by created_at desc, rowid desc limit 1""",
        (experiment_id(root),),
    ).fetchone()
    conn.close()
    return row[0] if row else autoexp_git(["rev-parse", "refs/heads/main"], root, capture=True)


def init_db(root=None):
    root = resolve_root(root)
    require_autoexp_git_repo(root)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    return root


RUN_COLUMNS = (
    "run_id", "experiment_id", "run_dir", "report_path", "output_hash", "capsule_hash",
    "script_name", "script_hash", "script_env_hash", "runtime_context_hash", "stage_commit",
    "status", "stage_status", "created_at", "source_snapshot_id", "parent_run_id",
    "trigger_id", "exit_code", "runner", "runner_identity", "started_at", "ended_at",
    "duration_ms", "failure_kind", "failure_message", "reproduces_run_id",
)


def insert_run(meta, root=None):
    root = resolve_root(root)
    row = {
        "experiment_id": experiment_id(root),
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
        **meta,
    }
    row["stage_status"] = json.dumps(row["stage_status"])
    conn = db()
    conn.execute(
        f"insert into runs({', '.join(RUN_COLUMNS)}) values({', '.join(':' + c for c in RUN_COLUMNS)})",
        row,
    )
    conn.commit()
    conn.close()


def update_run(meta, root=None):
    conn = db()
    existing = conn.execute("select * from runs where run_id = ?", (meta["run_id"],)).fetchone()
    if not existing:
        conn.close()
        raise ValueError(f"unknown run_id: {meta['run_id']}")
    row = dict(existing) | meta
    if not isinstance(row["stage_status"], str):
        row["stage_status"] = json.dumps(row["stage_status"])
    assignments = ", ".join(f"{col}=:{col}" for col in RUN_COLUMNS if col != "run_id")
    conn.execute(f"update runs set {assignments} where run_id=:run_id", row)
    conn.commit()
    conn.close()
