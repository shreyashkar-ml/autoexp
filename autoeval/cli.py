import json
from pathlib import Path

import typer

from .config import RepoPaths, ensure_repo_layout, ensure_user_layout, read_json, touch_state
from .connectors import (
    add_profile,
    connect_profile,
    disconnect_profile,
    list_profiles,
    remove_profile,
    set_auth_ref,
    set_profile_enabled,
)
from .evals import run_eval_suite
from .harness_tools import (
    append_lesson,
    append_review,
    check_command_guardrail,
    decide_mode,
    generate_feature_list,
    get_feature_status,
    set_feature_status,
    write_tool_catalog,
)
from .migrations import run_migrations
from .orchestrator import fork_run, intervene, resume_task, run_task, status
from .provider_launcher import launch_provider_run
from .provider_surface import read_provider_files, read_provider_result, write_provider_session
from .verifier import load_verifier_config, run_autocheck, sync_autocheck_map_from_verifier, verifier_template_text

app = typer.Typer(help="Minimal autoeval harness CLI")
mcp_app = typer.Typer(help="MCP lifecycle commands")
provider_app = typer.Typer(help="Provider session surface and inspection commands")
verifier_app = typer.Typer(help="Verifier mapping commands")
tools_app = typer.Typer(help="Tool-call interfaces exposed by autoeval for coding-agent loops")
app.add_typer(mcp_app, name="mcp")
app.add_typer(provider_app, name="provider")
app.add_typer(verifier_app, name="verifier")
app.add_typer(tools_app, name="tools")


def _paths(repo: Path) -> RepoPaths:
    return RepoPaths.from_repo(repo)


def _emit(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def run(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    task: str = typer.Option(...),
    provider: str = typer.Option("codex"),
    mode: str = typer.Option(..., help="planning or instant, selected from the inline workflow.decide_mode policy"),
    run_id: str | None = typer.Option(None),
    force: bool = typer.Option(False, "--force", help="Rewrite instruction artifacts instead of preserving existing files"),
    launch: bool = typer.Option(True, "--launch/--no-launch", help="Continue into provider execution after preparing the run"),
    sandbox_mode: str = typer.Option("workspace-write"),
    timeout_sec: int | None = typer.Option(None, help="Optional provider execution timeout in seconds; defaults to no timeout"),
    model: str | None = typer.Option(None),
    config_profile: str | None = typer.Option(None, "--profile"),
    extra_arg: list[str] = typer.Option([], "--extra-arg"),
    context_threshold: float = typer.Option(0.6),
    eval_profile: str = typer.Option("default"),
    require_eval_pass: bool = typer.Option(True, "--require-eval-pass/--no-require-eval-pass"),
    run_autocheck_now: bool = typer.Option(True, "--run-autocheck-now/--no-run-autocheck-now"),
    autocheck_timeout_sec: int = typer.Option(300),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    run_migrations(paths)
    try:
        result = run_task(
            paths=paths,
            task=task,
            provider=provider,
            mode=mode,
            run_id=run_id,
            context_threshold=context_threshold,
            eval_profile=eval_profile,
            require_eval_pass=require_eval_pass,
            run_autocheck_now=run_autocheck_now,
            autocheck_timeout_sec=autocheck_timeout_sec,
            force=force,
        )
    except Exception as exc:
        _emit({"ok": False, "provider": provider, "error": str(exc)})
        raise typer.Exit(code=1) from exc

    if not launch:
        payload = {**result, "ok": True, "provider_launch": None}
        _emit(payload)
        return

    try:
        provider_result = launch_provider_run(
            paths=paths,
            provider=provider,
            run_id=str(result["run_id"]),
            task=task,
            mode=str(result["mode"]),
            sandbox_mode=sandbox_mode,
            timeout_sec=timeout_sec,
            model=model,
            config_profile=config_profile,
            extra_args=extra_arg,
            session_file=str(result["provider_session_file"]),
        )
    except Exception as exc:
        _emit({**result, "ok": False, "error": str(exc)})
        raise typer.Exit(code=1) from exc

    payload = {**result, "ok": bool(provider_result.ok), "provider_launch": provider_result.model_dump()}
    _emit(payload)
    if not provider_result.ok:
        raise typer.Exit(code=1)


@app.command()
def resume(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    task: str = typer.Option("Continue target repository execution"),
    provider: str = typer.Option("codex"),
    mode: str | None = typer.Option(None, help="planning or instant; default keeps previous mode"),
    run_id: str | None = typer.Option(None),
    force: bool = typer.Option(False, "--force", help="Rewrite instruction artifacts instead of preserving existing files"),
    context_threshold: float = typer.Option(0.6),
    eval_profile: str = typer.Option("default"),
    require_eval_pass: bool = typer.Option(True, "--require-eval-pass/--no-require-eval-pass"),
    run_autocheck_now: bool = typer.Option(True, "--run-autocheck-now/--no-run-autocheck-now"),
    autocheck_timeout_sec: int = typer.Option(300),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    run_migrations(paths)
    try:
        result = resume_task(
            paths=paths,
            task=task,
            provider=provider,
            mode=mode,
            run_id=run_id,
            context_threshold=context_threshold,
            eval_profile=eval_profile,
            require_eval_pass=require_eval_pass,
            run_autocheck_now=run_autocheck_now,
            autocheck_timeout_sec=autocheck_timeout_sec,
            force=force,
        )
    except Exception as exc:
        _emit({"ok": False, "provider": provider, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _emit(result)


@app.command("status")
def status_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(status(paths, run_id=run_id))


@app.command("intervene")
def intervene_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    reason: str = typer.Option(...),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(intervene(paths, reason=reason, run_id=run_id))


@app.command("fork")
def fork_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    source_run_id: str = typer.Option(...),
    target_run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(fork_run(paths, source_run_id=source_run_id, target_run_id=target_run_id))


@app.command("eval")
def eval_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    run_id: str | None = typer.Option(None),
    profile: str = typer.Option("default"),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id")
    if not active_run:
        raise typer.BadParameter("no active run found; provide --run-id explicitly")
    _emit(run_eval_suite(paths=paths, run_id=active_run, profile=profile))


@app.command("autocheck")
def autocheck_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    run_id: str | None = typer.Option(None),
    sync_verifier_map: bool = typer.Option(
        True,
        "--sync-verifier-map/--no-sync-verifier-map",
        "--sync-feature-list/--no-sync-feature-list",
    ),
    update_feature_status: bool = typer.Option(True, "--update-feature-status/--no-update-feature-status"),
    selection_mode: str = typer.Option("feature-list", "--selection-mode", help="feature-list or all"),
    target: list[str] = typer.Option([], "--target", help="Explicit linked pytest target to run (repeatable)"),
    timeout_sec: int = typer.Option(300),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id") or f"autocheck_{task_safe_now()}"
    touch_state(paths, last_run_id=active_run)
    _emit(
        run_autocheck(
            paths=paths,
            run_id=active_run,
            sync_verifier_map=sync_verifier_map,
            update_feature_status=update_feature_status,
            selection_mode=selection_mode,
            targets=target,
            timeout_sec=timeout_sec,
        )
    )


def task_safe_now() -> str:
    from .config import utc_now_iso

    return utc_now_iso().replace(":", "").replace("-", "").replace("+", "_")


@verifier_app.command("sync")
def verifier_sync_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(sync_autocheck_map_from_verifier(paths))


@verifier_app.command("show")
def verifier_show_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(load_verifier_config(paths).model_dump())


@verifier_app.command("path")
def verifier_path_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    verifier_file = paths.verifier_file
    _emit({"path": str(verifier_file), "exists": verifier_file.exists()})


@verifier_app.command("template")
def verifier_template_cmd() -> None:
    typer.echo(verifier_template_text())


@provider_app.command("session")
def provider_session_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    provider: str = typer.Option("codex"),
    run_id: str | None = typer.Option(None),
    task: str | None = typer.Option(None),
    mode: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id")
    if not active_run:
        raise typer.BadParameter("no active run found; provide --run-id or create a run first")
    _emit(write_provider_session(paths=paths, run_id=str(active_run), provider=provider, task=task, mode=mode))


@provider_app.command("result")
def provider_result_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    provider: str = typer.Option("codex"),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id")
    if not active_run:
        raise typer.BadParameter("no active run found; provide --run-id or create a run first")
    try:
        payload = read_provider_result(paths=paths, run_id=str(active_run), provider=provider)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit(payload)


@provider_app.command("files")
def provider_files_cmd(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    provider: str = typer.Option("codex"),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id")
    if not active_run:
        raise typer.BadParameter("no active run found; provide --run-id or create a run first")
    try:
        payload = read_provider_files(paths=paths, run_id=str(active_run), provider=provider)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit(payload)


@mcp_app.command("list")
def mcp_list(
    scope: str = typer.Option("effective"),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit({"scope": scope, "profiles": list_profiles(paths, scope=scope)})


@mcp_app.command("add")
def mcp_add(
    scope: str = typer.Option(...),
    name: str = typer.Option(...),
    transport: str = typer.Option("stdio"),
    command: str = typer.Option(""),
    tool_namespace: str = typer.Option(""),
    required_env: str = typer.Option(""),
    timeout_s: int = typer.Option(60),
    enabled: bool = typer.Option(True),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    env_items = [item.strip() for item in required_env.split(",") if item.strip()]
    profile = add_profile(
        paths=paths,
        scope=scope,
        name=name,
        transport=transport,
        command=command,
        tool_namespace=tool_namespace,
        required_env=env_items,
        timeout_s=timeout_s,
        enabled=enabled,
    )
    _emit({"scope": scope, "name": name, "profile": profile})


@mcp_app.command("remove")
def mcp_remove(
    scope: str = typer.Option(...),
    name: str = typer.Option(...),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    removed = remove_profile(paths=paths, scope=scope, name=name)
    _emit({"scope": scope, "name": name, "removed": removed})


@mcp_app.command("enable")
def mcp_enable(
    scope: str = typer.Option(...),
    name: str = typer.Option(...),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    _emit(set_profile_enabled(paths=paths, scope=scope, name=name, enabled=True))


@mcp_app.command("disable")
def mcp_disable(
    scope: str = typer.Option(...),
    name: str = typer.Option(...),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    _emit(set_profile_enabled(paths=paths, scope=scope, name=name, enabled=False))


@mcp_app.command("connect")
def mcp_connect(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    name: str = typer.Option(...),
) -> None:
    paths = _paths(repo)
    _emit(connect_profile(paths=paths, name=name))


@mcp_app.command("disconnect")
def mcp_disconnect(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    name: str = typer.Option(...),
) -> None:
    paths = _paths(repo)
    _emit(disconnect_profile(paths=paths, name=name))


@mcp_app.command("set-auth")
def mcp_set_auth(
    name: str = typer.Option(...),
    auth_ref: str = typer.Option(...),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    _emit(set_auth_ref(paths=paths, name=name, auth_ref=auth_ref))


@tools_app.command("list")
def tools_list(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    payload = write_tool_catalog(paths)
    _emit(payload)


@tools_app.command("decide-mode")
def tools_decide_mode(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    request: str = typer.Option(..., help="User request text to classify"),
    mode: str = typer.Option(..., help="planning or instant, selected by the coding agent from the inline workflow.decide_mode policy"),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    decision = decide_mode(paths=paths, request=request, mode=mode, run_id=run_id)
    touch_state(paths, mode=decision["mode"], mode_decided_at=decision["created_at"])
    _emit(decision)


@tools_app.command("guardrail-check")
def tools_guardrail_check(
    repo: Path | None = typer.Option(None, exists=True, file_okay=False, dir_okay=True),
    command: str = typer.Option(...),
    target: str | None = typer.Option(None),
    no_network: bool = typer.Option(True, "--no-network/--allow-network"),
) -> None:
    del repo
    decision = check_command_guardrail(
        command=command,
        target=target,
        no_network=no_network,
        metadata={"source": "tools.guardrail-check"},
    )
    _emit(decision)
    if not bool(decision.get("allowed", False)):
        raise typer.Exit(code=1)


@tools_app.command("feature-status-set")
def tools_feature_status_set(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    task_id: str = typer.Option(..., "--task-id"),
    status: str = typer.Option(...),
    run_id: str | None = typer.Option(None),
    actor: str = typer.Option("coding_agent"),
    note: str = typer.Option(""),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    normalized = status.strip().lower()
    if normalized not in {"true", "false"}:
        raise typer.BadParameter("status must be true or false")
    _emit(
        set_feature_status(
            paths=paths,
            task_id=task_id,
            status=(normalized == "true"),
            run_id=run_id,
            actor=actor,
            note=note,
        )
    )


@tools_app.command("feature-list-generate")
def tools_feature_list_generate(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    input_json: str = typer.Option(..., help="Full feature_list payload as JSON"),
    run_id: str | None = typer.Option(None),
    actor: str = typer.Option("coding_agent"),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(generate_feature_list(paths=paths, input_json=input_json, run_id=run_id, actor=actor))


@tools_app.command("feature-status-get")
def tools_feature_status_get(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    task_id: str = typer.Option(..., "--task-id"),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(get_feature_status(paths=paths, task_id=task_id))


@tools_app.command("autocheck")
def tools_autocheck(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    run_id: str | None = typer.Option(None),
    sync_verifier_map: bool = typer.Option(
        True,
        "--sync-verifier-map/--no-sync-verifier-map",
        "--sync-feature-list/--no-sync-feature-list",
    ),
    update_feature_status: bool = typer.Option(True, "--update-feature-status/--no-update-feature-status"),
    selection_mode: str = typer.Option("feature-list", "--selection-mode", help="feature-list or all"),
    target: list[str] = typer.Option([], "--target", help="Explicit linked pytest target to run (repeatable)"),
    timeout_sec: int = typer.Option(300),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id") or f"autocheck_{task_safe_now()}"
    touch_state(paths, last_run_id=active_run)
    _emit(
        run_autocheck(
            paths=paths,
            run_id=active_run,
            sync_verifier_map=sync_verifier_map,
            update_feature_status=update_feature_status,
            selection_mode=selection_mode,
            targets=target,
            timeout_sec=timeout_sec,
        )
    )


@tools_app.command("run-status")
def tools_run_status(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(status(paths, run_id=run_id))


@tools_app.command("run-eval")
def tools_run_eval(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    run_id: str | None = typer.Option(None),
    profile: str = typer.Option("default"),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = run_id or state.get("last_run_id")
    if not active_run:
        raise typer.BadParameter("no active run found; provide --run-id explicitly")
    _emit(run_eval_suite(paths=paths, run_id=active_run, profile=profile))


@tools_app.command("append-lesson")
def tools_append_lesson(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    text: str = typer.Option(...),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(append_lesson(paths=paths, text=text, run_id=run_id))


@tools_app.command("append-review")
def tools_append_review(
    repo: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    text: str = typer.Option(...),
    run_id: str | None = typer.Option(None),
) -> None:
    paths = _paths(repo)
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    _emit(append_review(paths=paths, text=text, run_id=run_id))


if __name__ == "__main__":
    app()
