import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_autoeval(args, cwd, check=True):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)

    return subprocess.run(
        [sys.executable, "-m", "autoeval", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_start_creates_visible_project(tmp_path):
    project = tmp_path / "demo_eval"

    result = run_autoeval(["start", str(project), "--title", "Demo Eval"], tmp_path)

    assert "started autoeval project" in result.stdout
    assert (project / "autoeval.json").is_file()
    assert (project / "autoeval.md").is_file()
    assert (project / "input" / "params.json").is_file()
    assert (project / "input" / "params.schema.json").is_file()
    assert (project / "script" / "script.py").is_file()
    assert (project / "report" / "report.py").is_file()
    assert (project / ".git").is_dir()

    config = json.loads((project / "autoeval.json").read_text())
    assert config["title"] == "Demo Eval"

    status = subprocess.check_output(["git", "-C", str(project), "status", "--short"], text=True)
    assert status == ""


def test_commands_work_from_project_subdirectory(tmp_path):
    project = tmp_path / "demo_eval"
    run_autoeval(["start", str(project)], tmp_path)

    result = run_autoeval(["hash"], project / "input")

    assert "capsule_hash:" in result.stdout
    assert "input_hash:" in result.stdout


def test_storage_versions_are_explicit(tmp_path):
    project = tmp_path / "demo_eval"
    run_autoeval(["start", str(project)], tmp_path)

    run_autoeval(["storage", "--label", "initial"], project)

    conn = sqlite3.connect(project / "index.sqlite")
    assert scalar(conn, "select count(*) from input_versions") == 1
    assert scalar(conn, "select count(*) from script_versions") == 1
    assert scalar(conn, "select count(*) from report_versions") == 1
    assert scalar(conn, "select label from input_versions") == "initial"

    params_path = project / "input" / "params.json"
    params = json.loads(params_path.read_text())
    params["message"] = "changed"
    params_path.write_text(json.dumps(params, indent=2) + "\n")

    run_autoeval(["storage", "--label", "changed-input"], project)

    assert scalar(conn, "select count(*) from input_versions") == 2
    assert scalar(conn, "select count(*) from script_versions") == 1
    assert scalar(conn, "select count(*) from report_versions") == 1


def test_run_without_docker_does_not_store_versions(tmp_path, monkeypatch):
    project = tmp_path / "demo_eval"
    run_autoeval(["start", str(project)], tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(bin_dir))

    result = run_autoeval(["run"], project, check=False)

    assert result.returncode == 1
    assert "required command not found: docker" in result.stderr
    assert not any((project / "runs").iterdir())
