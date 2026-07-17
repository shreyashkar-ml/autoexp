import hashlib
import threading
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

import autoexp.importer as importer
from autoexp.artifacts import list_artifacts
from autoexp.autoresearch import AutoResearch
from autoexp.cli import relink_cmd
from autoexp.execution import execute
from autoexp.runs import copy_run_source, restore_run_state
from autoexp.runtime import run_source
from autoexp.server import AutoexpHTTPServer, AutoexpHandler, view
from autoexp.snapshots import (
    _hash_declared_source, capture_workspace, materialize_snapshot,
    snapshot_hashes, snapshot_matches,
)
from autoexp.store import db
from autoexp.workspace import (
    create_experiment, declare_file, experiment_entry, register_repository, run_dir_for,
)


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


def test_research_restore_only_reverts_agent_subject(tmp_path, monkeypatch):
    entry = research_experiment(tmp_path, monkeypatch)
    research = AutoResearch(entry["experiment_id"])
    contract = research.state()["contract"]
    repo = Path(entry["repo_path"])
    (repo / "candidate.py").write_text("print('changed candidate')\n")
    candidate = capture_workspace(
        entry["experiment_id"], parent_snapshot_id=contract["best_snapshot_id"]
    )
    (repo / "program.md").write_text("user changed the program\n")

    research._restore_snapshot(
        contract["best_snapshot_id"], contract["subject_path"], candidate["snapshot_id"]
    )

    assert (repo / "candidate.py").read_text() == "print('candidate')\n"
    assert (repo / "program.md").read_text() == "user changed the program\n"

    (repo / "candidate.py").write_text("print('newer candidate')\n")
    with pytest.raises(ValueError, match="newer candidate changes"):
        research._restore_snapshot(
            contract["best_snapshot_id"], contract["subject_path"], candidate["snapshot_id"]
        )
    assert (repo / "candidate.py").read_text() == "print('newer candidate')\n"


def test_restore_refuses_modified_ignored_source(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "main.py").write_text("print('ok')\n")
    (repo / "ignored.txt").write_text("snapshot value\n")
    entry = create_experiment("test ignored restore", root=repo, entrypoint="main.py")
    declare_file(entry["experiment_id"], "ignored.txt", "supporting-source")
    commit_repo(repo, "initial")
    run = execute(entry["experiment_id"])

    (repo / "ignored.txt").write_text("uncommitted ignored value\n")
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout == ""
    with pytest.raises(ValueError, match="ignored.txt"):
        restore_run_state(run["run_id"], entry["experiment_id"])


def test_only_secret_environment_values_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / ".env").write_text("DEBUG=1\n")
    (repo / "main.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['AUTOEXP_OUTPUT_DIR'], 'metrics.json').write_text(json.dumps({\n"
        "    'tag': os.environ['AUTOEXP_RESEARCH_TAG'],\n"
        "    'budget': os.environ['AUTOEXP_RESEARCH_BUDGET_SEC'],\n"
        "    'debug': os.environ['DEBUG'],\n"
        "    'token': os.environ['API_TOKEN'],\n"
        "}))\n"
    )
    entry = create_experiment(
        "test selective redaction",
        root=repo,
        entrypoint="main.py",
        config={"external_inputs": [{"name": "API_TOKEN", "kind": "secret"}]},
    )
    run = execute(entry["experiment_id"], environment={
        "AUTOEXP_RESEARCH_TAG": "a01",
        "AUTOEXP_RESEARCH_BUDGET_SEC": "300",
        "API_TOKEN": "abc123",
    })

    metrics = json.loads(
        (run_dir_for(run, entry["experiment_id"]) / "output/metrics.json").read_text()
    )
    assert metrics == {
        "tag": "a01",
        "budget": "300",
        "debug": "1",
        "token": "[redacted]",
    }


def test_timeout_failure_message_redacts_short_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    (repo / "main.py").write_text("print('ok')\n")
    entry = create_experiment(
        "test timeout redaction",
        root=repo,
        entrypoint="main.py",
        config={"external_inputs": [{"name": "API_TOKEN", "kind": "secret"}]},
    )

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["docker", "run", "-e", "API_TOKEN=abc123"], 1
        )

    monkeypatch.setattr("autoexp.execution.run_script_local", timeout)
    run = execute(entry["experiment_id"], environment={"API_TOKEN": "abc123"})

    assert run["status"] == "failed"
    assert run["failure_kind"] == "timeout"
    assert "abc123" not in run["failure_message"]
    assert "[redacted]" in run["failure_message"]


def _old_snapshot_hashes(root, config):
    script = hashlib.sha256()
    for path in sorted((root / "experiment").rglob("*")):
        if path.is_file():
            script.update(path.relative_to(root / "experiment").as_posix().encode())
            script.update(b"\0")
            script.update(path.read_bytes())
            script.update(b"\0")

    def file_hash(path):
        return hashlib.sha256(path.read_bytes() if path.is_file() else b"").hexdigest()

    runtime = {
        key: config[key]
        for key in ("runner", "sandbox", "runtime")
        if key in config
    }
    hashes = {
        "script_hash": script.hexdigest(),
        "params_hash": file_hash(root / ".autoexp/params.json"),
        "manifest_hash": file_hash(root / ".autoexp/stage.json"),
        "runtime_config_hash": hashlib.sha256(
            json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    hashes["source_hash"] = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return hashes


def test_imports_and_executes_real_legacy_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    source = git_repo(tmp_path / "legacy")
    control = source / ".autoexp"
    experiment = source / "experiment"
    control.mkdir()
    experiment.mkdir()
    config = {
        "title": "legacy",
        "description": "A real 0.2 layout",
        "runner": "local",
        "sandbox": {
            "image": "python:3.12-slim",
            "network": "none",
            "cpus": "1",
            "memory": "512m",
        },
        "runtime": {},
        "report_instruction_file": ".autoexp/custom-report.md",
        "source": {"root": "experiment", "editable": ["main.py"]},
    }
    stage = {
        "name": "main.py",
        "command": "python main.py --ctx ${CTX}",
        "working_dir": "experiment",
        "interface_version": "1",
    }
    (control / "project.json").write_text(json.dumps(config))
    (control / "stage.json").write_text(json.dumps(stage))
    (control / "params.json").write_text("{}")
    (control / "params.schema.json").write_text(
        json.dumps({"type": "object", "properties": {}})
    )
    (control / "instructions.md").write_text("Legacy agent guidance.\n")
    (control / "custom-report.md").write_text("Keep this custom report guidance.\n")
    (experiment / "main.py").write_text("print('legacy run')\n")
    commit_repo(source, "legacy snapshot")
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(source), str(control / "repository")],
        check=True,
    )

    hashes = _old_snapshot_hashes(source, config)
    old = sqlite3.connect(control / "state.sqlite")
    old.execute(
        """create table source_snapshots(
             snapshot_id text primary key, project_id text not null,
             parent_snapshot_id text, git_commit text not null,
             script_hash text not null, params_hash text not null,
             manifest_hash text not null, runtime_config_hash text not null,
             source_hash text not null, created_at text not null,
             created_by_trigger_id text, label text, legacy_run_id text
           )"""
    )
    old.execute(
        """insert into source_snapshots values(
             'legacy_snapshot', 'legacy_project', null, ?, ?, ?, ?, ?, ?,
             '2025-01-01T00-00-00Z', null, 'Legacy snapshot', null
           )""",
        (
            commit,
            hashes["script_hash"],
            hashes["params_hash"],
            hashes["manifest_hash"],
            hashes["runtime_config_hash"],
            hashes["source_hash"],
        ),
    )
    old.commit()
    old.close()

    summary = importer.import_legacy_project(source)
    experiment_id = summary["experiment_id"]
    assert summary["validated"]["snapshot_hashes"] == {"checked": 1, "ok": True}
    assert experiment_entry(experiment_id)["report_guidance"] == "Keep this custom report guidance.\n"

    restored = tmp_path / "restored-legacy"
    materialize_snapshot("legacy_snapshot", restored, experiment_id)
    assert snapshot_matches(hashes, restored)
    run = execute(experiment_id, snapshot_id="legacy_snapshot")
    assert run["status"] == "success"
    assert [item["path"] for item in run_source(run["run_id"], experiment_id)["files"]] == [
        "experiment/main.py"
    ]


def test_downloads_stream_complete_artifact_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOEXP_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    output_size = 16 * 1024 * 1024 + 257
    (repo / "main.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"Path(os.environ['AUTOEXP_OUTPUT_DIR'], 'large.bin').write_bytes(b'x' * {output_size})\n"
        "print('L' * 70000)\n"
    )
    entry = create_experiment("test complete downloads", root=repo, entrypoint="main.py")
    run = execute(entry["experiment_id"])
    artifact = next(
        item for item in list_artifacts(run["run_id"], entry["experiment_id"], "output")
        if item["path"] == "output/large.bin"
    )

    server = AutoexpHTTPServer(("127.0.0.1", 0), AutoexpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/api/runs/{run['run_id']}"
    try:
        with urlopen(
            f"{base}/artifacts/{artifact['artifact_id']}/content?download=1",
            timeout=30,
        ) as response:
            assert response.read() == b"x" * output_size
        with urlopen(f"{base}/logs/stdout?download=1", timeout=30) as response:
            assert response.read() == b"L" * 70000 + b"\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
