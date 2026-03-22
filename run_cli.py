import json
from pathlib import Path

from typer.testing import CliRunner

import autoeval.cli as cli_module
from autoeval.config import RepoPaths, write_json
from autoeval.provider_surface import ProviderExecutionResult
from autoeval.provider_surface import provider_result_file
from autoeval.terminal_ui import launch_terminal_ui


runner = CliRunner()


def _parse_last_json_payload(output: str) -> dict:
    start = output.rfind("{\n")
    if start == -1:
        raise AssertionError(f"json payload missing in output: {output}")
    return json.loads(output[start:])


def _write_repo_verifier(repo: Path, text: str = "schema_version: 1\ntests: []\n") -> Path:
    verifier_file = repo / "verifier.yaml"
    verifier_file.write_text(text, encoding="utf-8")
    return verifier_file


def _invoke_run(repo, *extra_args: str):
    return runner.invoke(
        cli_module.app,
        [
            "run",
            "--repo",
            str(repo),
            "--task",
            "Implement feature set",
            "--mode",
            "planning",
            "--no-run-autocheck-now",
            *extra_args,
        ],
    )


def _invoke_resume(repo, *extra_args: str):
    return runner.invoke(
        cli_module.app,
        [
            "resume",
            "--repo",
            str(repo),
            "--no-run-autocheck-now",
            *extra_args,
        ],
    )


def test_run_without_force_preserves_existing_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    first = _invoke_run(repo, "--no-launch")
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["ok"] is True
    assert first_payload["provider_launch"] is None

    research_file = repo / ".autoeval" / "instructions" / "research.md"
    research_file.write_text("keep this content\n", encoding="utf-8")

    second = _invoke_run(repo, "--no-launch")
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["bootstrap"]["skipped"]
    assert str(research_file) in second_payload["bootstrap"]["skipped"]
    assert research_file.read_text(encoding="utf-8") == "keep this content\n"


def test_run_with_force_rewrites_instruction_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    first = _invoke_run(repo, "--no-launch")
    assert first.exit_code == 0, first.output

    research_file = repo / ".autoeval" / "instructions" / "research.md"
    research_file.write_text("overwrite me\n", encoding="utf-8")

    forced = _invoke_run(repo, "--no-launch", "--force")
    assert forced.exit_code == 0, forced.output
    forced_payload = json.loads(forced.output)
    assert str(research_file) in forced_payload["bootstrap"]["created"]
    assert research_file.read_text(encoding="utf-8") == ""


def test_resume_without_force_preserves_existing_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    first = _invoke_run(repo, "--no-launch")
    assert first.exit_code == 0, first.output

    research_file = repo / ".autoeval" / "instructions" / "research.md"
    research_file.write_text("keep this content\n", encoding="utf-8")

    resumed = _invoke_resume(repo)
    assert resumed.exit_code == 0, resumed.output
    resumed_payload = json.loads(resumed.output)
    assert resumed_payload["bootstrap"]["skipped"]
    assert str(research_file) in resumed_payload["bootstrap"]["skipped"]
    assert research_file.read_text(encoding="utf-8") == "keep this content\n"


def test_resume_with_force_rewrites_instruction_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    first = _invoke_run(repo, "--no-launch")
    assert first.exit_code == 0, first.output

    research_file = repo / ".autoeval" / "instructions" / "research.md"
    research_file.write_text("overwrite me\n", encoding="utf-8")

    resumed = _invoke_resume(repo, "--force")
    assert resumed.exit_code == 0, resumed.output
    resumed_payload = json.loads(resumed.output)
    assert str(research_file) in resumed_payload["bootstrap"]["created"]
    assert research_file.read_text(encoding="utf-8") == ""


def test_run_launches_provider_by_default(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    calls = []

    def fake_launch_provider_run(**kwargs):
        calls.append(kwargs)
        return ProviderExecutionResult(
            ok=True,
            provider="codex",
            transport="stub",
            command=["codex", "stub"],
            session_file=str(kwargs["session_file"]),
            prompt_file=str(repo / "prompt.txt"),
            raw_trace_file=str(repo / "raw_trace.jsonl"),
            normalized_trace_file=str(repo / "normalized_trace.jsonl"),
            last_message_file=str(repo / "last_message.txt"),
            final_output="stub output",
        )

    monkeypatch.setattr(cli_module, "launch_provider_run", fake_launch_provider_run)

    result = _invoke_run(repo)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["provider_launch"]["transport"] == "stub"
    assert len(calls) == 1
    assert calls[0]["sandbox_mode"] == "workspace-write"
    assert calls[0]["timeout_sec"] is None
    assert calls[0]["run_id"] == payload["run_id"]


def test_init_command_is_removed():
    init_result = runner.invoke(cli_module.app, ["init"])
    assert init_result.exit_code != 0
    assert "No such command 'init'" in init_result.output


def test_provider_help_lists_session_without_launch():
    help_result = runner.invoke(cli_module.app, ["provider", "--help"])

    assert help_result.exit_code == 0, help_result.output
    assert "Provider session surface and inspection commands" in help_result.output
    assert "result" in help_result.output
    assert "session" in help_result.output
    assert "launch" not in help_result.output.lower()


def test_provider_launch_is_absent_but_provider_session_exists():
    launch_result = runner.invoke(cli_module.app, ["provider", "launch"])
    assert launch_result.exit_code != 0
    assert "No such command 'launch'" in launch_result.output

    session_result = runner.invoke(cli_module.app, ["provider", "session", "--help"])
    assert session_result.exit_code == 0, session_result.output
    assert "--repo" in session_result.output


def test_provider_result_reads_saved_provider_execution_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    run_result = _invoke_run(repo, "--no-launch")
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.output)
    run_id = run_payload["run_id"]

    paths = RepoPaths.from_repo(repo)
    expected = {
        "ok": True,
        "provider": "codex",
        "transport": "stub",
        "command": ["codex", "exec"],
        "session_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "provider_session.json"),
        "prompt_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_prompt.txt"),
        "raw_trace_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_raw_trace.jsonl"),
        "normalized_trace_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_normalized_trace.jsonl"),
        "last_message_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_last_message.txt"),
        "exit_code": 0,
        "final_output": "done",
        "error": None,
        "event_count": 2,
        "created_at": "2026-03-20T14:30:00+00:00",
        "metadata": {"source": "test"},
    }
    write_json(provider_result_file(paths, run_id, "codex"), expected)

    result = runner.invoke(cli_module.app, ["provider", "result", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == expected


def test_provider_files_reports_known_artifact_paths_for_active_run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    run_result = _invoke_run(repo, "--no-launch")
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.output)
    run_id = run_payload["run_id"]

    result = runner.invoke(cli_module.app, ["provider", "files", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "run_id": run_id,
        "provider": "codex",
        "session_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "provider_session.json"),
        "prompt_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_prompt.txt"),
        "raw_trace_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_raw_trace.jsonl"),
        "normalized_trace_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_normalized_trace.jsonl"),
        "last_message_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_last_message.txt"),
        "result_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "codex_result.json"),
    }


def test_provider_files_uses_run_provider_and_lists_optional_outputs_before_creation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    run_result = _invoke_run(repo, "--provider", "acme", "--no-launch")
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.output)
    run_id = run_payload["run_id"]

    result = runner.invoke(cli_module.app, ["provider", "files", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "run_id": run_id,
        "provider": "acme",
        "session_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "provider_session.json"),
        "prompt_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "acme_prompt.txt"),
        "raw_trace_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "acme_raw_trace.jsonl"),
        "normalized_trace_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "acme_normalized_trace.jsonl"),
        "last_message_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "acme_last_message.txt"),
        "result_file": str(repo / ".autoeval" / "runs" / run_id / "provider" / "acme_result.json"),
    }
    assert Path(payload["session_file"]).exists() is True
    assert Path(payload["prompt_file"]).exists() is False
    assert Path(payload["raw_trace_file"]).exists() is False
    assert Path(payload["normalized_trace_file"]).exists() is False
    assert Path(payload["last_message_file"]).exists() is False
    assert Path(payload["result_file"]).exists() is False


def test_provider_result_reports_missing_saved_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))
    _write_repo_verifier(repo)

    run_result = _invoke_run(repo, "--no-launch")
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.output)

    result = runner.invoke(cli_module.app, ["provider", "result", "--repo", str(repo)])

    assert result.exit_code != 0
    assert "no saved provider result found" in result.output
    assert "launch the provider first" in result.output


def test_run_requires_repo_root_verifier_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))

    result = _invoke_run(repo, "--no-launch")

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "missing verifier.yaml" in payload["error"]
    assert str(repo / "verifier.yaml") in payload["error"]
    assert (repo / ".autoeval" / "verifier.yaml").exists() is False


def test_verifier_path_reports_missing_repo_root_verifier_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(cli_module.app, ["verifier", "path", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "exists": False,
        "path": str(repo / "verifier.yaml"),
    }
    assert (repo / "verifier.yaml").exists() is False


def test_verifier_path_reports_existing_repo_root_verifier_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    verifier_file = _write_repo_verifier(repo)

    result = runner.invoke(cli_module.app, ["verifier", "path", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "exists": True,
        "path": str(verifier_file),
    }


def test_root_invocation_opens_rich_ui_and_returns_query_payload(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))

    result = runner.invoke(
        cli_module.app,
        ["--repo", str(repo)],
        input="add pytorch conv support\ncodex\ngpt-5\nworkspace-write\n\n",
    )

    assert result.exit_code == 0, result.output
    payload = _parse_last_json_payload(result.output)
    assert payload["ok"] is True
    assert payload["path"] == "/query"
    assert payload["provider"] == "codex"
    assert payload["model"] == "gpt-5"


def test_ui_autoresearch_path_is_placeholder(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))

    result = runner.invoke(cli_module.app, ["ui", "--repo", str(repo)], input="/autoresearch investigate\n")

    assert result.exit_code == 0, result.output
    payload = _parse_last_json_payload(result.output)
    assert payload["ok"] is False
    assert payload["reason"] == "placeholder_autoresearch"


def test_ui_non_codex_provider_is_placeholder(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))

    result = runner.invoke(
        cli_module.app,
        ["ui", "--repo", str(repo)],
        input="add support\nclaude-code\n",
    )

    assert result.exit_code == 0, result.output
    payload = _parse_last_json_payload(result.output)
    assert payload["ok"] is False
    assert payload["reason"] == "placeholder_provider"
    assert payload["provider"] == "claude-code"


def test_ui_rejects_unknown_slash_command(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))

    result = runner.invoke(cli_module.app, ["ui", "--repo", str(repo)], input="/foo demo\n")

    assert result.exit_code != 0
    assert "unsupported path '/foo'" in result.output


def test_launch_terminal_ui_accepts_empty_extra_args_input_without_prompting(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AUTOEVAL_HOME", str(tmp_path / "home"))

    payload = launch_terminal_ui(
        repo=repo,
        execute=False,
        task_input="add support",
        provider_input="codex",
        model_input="<provider-default>",
        sandbox_input="workspace-write",
        extra_args_input="",
    )

    assert payload["ok"] is True
    assert payload["extra_args"] == []
