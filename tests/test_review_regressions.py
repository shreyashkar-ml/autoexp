import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

import autoexp.importer as importer
from autoexp.autoresearch import AutoResearch
from autoexp.cli import relink_cmd
from autoexp.execution import execute
from autoexp.runs import copy_run_source, restore_run_state
from autoexp.server import view
from autoexp.snapshots import (
    _hash_declared_source, capture_workspace, materialize_snapshot,
    snapshot_hashes, snapshot_matches,
)
from autoexp.store import db
from autoexp.workspace import create_experiment, declare_file, register_repository


def git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def commit_repo(repo, message):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.com", "commit", "-qm", message,
        ],
        check=True,
    )


def test_snapshot_drops_files_reclassified_as_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / "main.py").write_text("print('ok')\n")
    (repo / "credentials.txt").write_text("old-secret\n")
    entry = create_experiment("test snapshots", root=repo, entrypoint="main.py")
    declare_file(entry["experiment_id"], "credentials.txt", "supporting-source")
    first = capture_workspace(entry["experiment_id"])

    declare_file(entry["experiment_id"], "credentials.txt", "secret-source")
    second = capture_workspace(
        entry["experiment_id"], parent_snapshot_id=first["snapshot_id"]
    )
    restored = tmp_path / "restored"
    materialize_snapshot(second["snapshot_id"], restored, entry["experiment_id"])

    assert not (restored / "credentials.txt").exists()
    legacy = snapshot_hashes(restored, include_types=False)
    assert legacy["source_hash"] != second["source_hash"]
    assert snapshot_matches(legacy, restored)


def test_source_hash_distinguishes_empty_missing_and_directory(tmp_path):
    config = {"files": [{"path": "source.txt", "role": "editable-source"}]}
    source = tmp_path / "source.txt"
    source.write_text("")
    empty = _hash_declared_source(tmp_path, config)
    source.unlink()
    missing = _hash_declared_source(tmp_path, config)
    source.mkdir()
    directory = _hash_declared_source(tmp_path, config)

    assert len({empty, missing, directory}) == 3


def test_relinked_repository_keeps_its_original_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / "main.py").write_text("print('ok')\n")
    entry = create_experiment("test relink", root=repo, entrypoint="main.py")
    moved = tmp_path / "moved"
    repo.rename(moved)

    relink_cmd(SimpleNamespace(repo_id=entry["repo_id"], path=str(moved)))
    registered = register_repository(moved)

    assert registered["repo_id"] == entry["repo_id"]


def research_experiment(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "research")
    (repo / "program.md").write_text("Improve the score.\n")
    (repo / "candidate.py").write_text("print('candidate')\n")
    (repo / "evaluate.py").write_text("print('evaluate')\n")
    config = {
        "autoresearch": {
            "objective": {
                "metric": "score",
                "direction": "max",
                "baseline": None,
                "budget_sec": 30,
            },
            "files": [
                {"path": "program.md", "role": "human"},
                {"path": "candidate.py", "role": "agent"},
                {"path": "evaluate.py", "role": "frozen"},
            ],
            "metric": {"kind": "json", "path": "metrics.json", "key": "score"},
            "agent": {"cmd": [sys.executable, "-c", "pass"]},
        }
    }
    entry = create_experiment(
        "test recovery",
        root=repo,
        kind="autoresearch",
        entrypoint="candidate.py",
        command="python candidate.py --ctx ${CTX}",
        config=config,
    )
    declare_file(entry["experiment_id"], "program.md", "supporting-source")
    declare_file(entry["experiment_id"], "evaluate.py", "frozen-evaluator")
    return entry


def test_stranded_research_attempt_is_recovered(tmp_path, monkeypatch):
    entry = research_experiment(tmp_path, monkeypatch)
    research = AutoResearch(entry["experiment_id"])
    contract = research.state()["contract"]
    conn = db()
    conn.execute(
        """insert into research_attempts(
             contract_id, attempt_id, sequence, status, hypothesis,
             base_snapshot_id, candidate_snapshot_id, metadata
           ) values (?, 'a01', 1, 'running', 'interrupted', ?, ?, ?)""",
        (
            contract["contract_id"],
            contract["best_snapshot_id"],
            contract["best_snapshot_id"],
            json.dumps({"owner_pid": 0}),
        ),
    )
    conn.commit()
    conn.close()

    AutoResearch(entry["experiment_id"])

    conn = db()
    attempt = conn.execute(
        "select status, failure_message from research_attempts where attempt_id = 'a01'"
    ).fetchone()
    conn.close()
    assert attempt["status"] == "failed"
    assert "no longer running" in attempt["failure_message"]


def test_restore_is_exact_and_refuses_dirty_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / "main.py").write_text("print('snapshot')\n")
    entry = create_experiment("test restore", root=repo, entrypoint="main.py")
    declare_file(entry["experiment_id"], "optional.txt", "supporting-source")
    commit_repo(repo, "initial")
    run = execute(entry["experiment_id"])

    (repo / "optional.txt").write_text("added later\n")
    commit_repo(repo, "add optional source")
    restore_run_state(run["run_id"], entry["experiment_id"])
    assert not (repo / "optional.txt").exists()

    commit_repo(repo, "restore snapshot")
    (repo / "main.py").write_text("print('dirty')\n")
    with pytest.raises(ValueError, match="uncommitted source changes"):
        restore_run_state(run["run_id"], entry["experiment_id"])


def test_restore_rejects_snapshot_paths_outside_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / "main.py").write_text("print('ok')\n")
    entry = create_experiment("test safe restore", root=repo, entrypoint="main.py")
    snapshot = tmp_path / "snapshot"
    (snapshot / ".autoexp").mkdir(parents=True)
    (snapshot / ".autoexp/project.json").write_text(json.dumps({
        "files": [{"path": "../outside.txt", "role": "editable-source"}],
    }))

    with pytest.raises(ValueError, match="snapshot source path"):
        copy_run_source(snapshot, entry["experiment_id"])


def test_terminal_run_rejects_new_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / "main.py").write_text("print('ok')\n")
    entry = create_experiment("test immutable evidence", root=repo, entrypoint="main.py")
    run = execute(entry["experiment_id"])
    conn = db()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            conn.execute(
                "insert into artifacts values (?, ?, 'output', ?, ?, ?, ?, ?, '{}')",
                ("late", run["run_id"], "output/late.txt", "text/plain", "0" * 64, 0, "now"),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            conn.execute(
                "insert into run_external_inputs values (?, ?, ?, ?, ?, ?, ?, '{}')",
                (run["run_id"], "late", "env", 1, "hash", "1", "pinned"),
            )
    finally:
        conn.close()


def test_failed_import_cleans_up_and_can_retry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("AUTOEXP_HOME", str(home))
    source = tmp_path / "legacy"
    control = source / ".autoexp"
    control.mkdir(parents=True)
    (control / "project.json").write_text(json.dumps({"title": "legacy", "runner": "local"}))
    sqlite3.connect(control / "state.sqlite").close()
    subprocess.run(["git", "init", "--bare", "-q", str(control / "repository")], check=True)

    original = importer._validate_artifact_hashes

    def fail_validation(*_args):
        raise ValueError("invalid legacy evidence")

    monkeypatch.setattr(importer, "_validate_artifact_hashes", fail_validation)
    with pytest.raises(ValueError, match="invalid legacy evidence"):
        importer.import_legacy_project(source)

    conn = db()
    assert conn.execute("select count(*) from experiments").fetchone()[0] == 0
    conn.close()
    assert not list(home.glob("repos/*/experiments/*"))

    monkeypatch.setattr(importer, "_validate_artifact_hashes", original)
    summary = importer.import_legacy_project(source)
    assert summary["experiment_id"]


def test_view_rejects_non_loopback_bind():
    with pytest.raises(ValueError, match="loopback"):
        view(host="0.0.0.0", open_browser=False)
