import json
import subprocess
import sys
from types import SimpleNamespace

from autoexp.autoresearch import AutoResearch
from autoexp.cli import relink_cmd
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
