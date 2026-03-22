import shlex
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import RepoPaths, read_json

QUERY_PATH = "/query"
AUTORESEARCH_PATH = "/autoresearch"


@dataclass(frozen=True)
class ProviderOption:
    name: str
    integrated: bool
    description: str


def provider_options() -> list[ProviderOption]:
    return [
        ProviderOption(name="codex", integrated=True, description="Integrated provider adapter (available now)."),
        ProviderOption(name="claude-code", integrated=False, description="Placeholder only (not integrated yet)."),
        ProviderOption(name="opencode", integrated=False, description="Placeholder only (not integrated yet)."),
    ]


def codex_model_options(paths: RepoPaths) -> list[str]:
    state = read_json(paths.state_file, {})
    candidates: list[str] = []

    last_model = str(state.get("model", "")).strip() if isinstance(state, dict) else ""
    if last_model:
        candidates.append(last_model)

    for value in ["gpt-5", "gpt-5-codex", "gpt-5-mini"]:
        if value not in candidates:
            candidates.append(value)

    if "<provider-default>" not in candidates:
        candidates.append("<provider-default>")
    return candidates


def sandbox_mode_options() -> list[str]:
    return ["workspace-write", "danger-full-access", "read-only"]


def _split_extra_args(raw: str) -> list[str]:
    return shlex.split(raw.strip()) if raw.strip() else []


def _prompt_or_value(value: str | None, prompt: str, *, default: str | None = None) -> str:
    if value is not None:
        return value
    if default is None:
        return typer.prompt(prompt)
    return typer.prompt(prompt, default=default)


def launch_terminal_ui(
    *,
    repo: Path,
    execute: bool,
    task_input: str | None = None,
    provider_input: str | None = None,
    model_input: str | None = None,
    sandbox_input: str | None = None,
    extra_args_input: str | None = None,
) -> dict[str, object]:
    console = Console()
    paths = RepoPaths.from_repo(repo)

    console.print(Panel.fit("[bold blue]AUTOEVAL[/bold blue] rich terminal UI", border_style="blue"))
    console.print("Default path: [bold]/query[/bold]   Optional placeholder: [bold]/autoresearch[/bold]\n")

    request = _prompt_or_value(task_input, "Type your request or command").strip()
    if not request:
        raise typer.BadParameter("request cannot be empty")

    command, _, remainder = request.partition(" ")
    if command == AUTORESEARCH_PATH:
        console.print("[yellow]/autoresearch is currently a placeholder path and is not implemented yet.[/yellow]")
        return {"ok": False, "reason": "placeholder_autoresearch", "path": AUTORESEARCH_PATH}

    selected_path = QUERY_PATH
    if request.startswith("/"):
        if command != QUERY_PATH:
            raise typer.BadParameter(
                f"unsupported path '{command}'. choose from: {QUERY_PATH}, {AUTORESEARCH_PATH}"
            )
        normalized_task = remainder.strip()
    else:
        normalized_task = request
    if not normalized_task:
        raise typer.BadParameter("a non-empty task is required for /query")

    options = provider_options()
    provider_names = [item.name for item in options]
    provider = _prompt_or_value(provider_input, "Provider", default="codex").strip().lower()
    if provider not in provider_names:
        raise typer.BadParameter(f"unsupported provider '{provider}'. choose from: {', '.join(provider_names)}")

    selected_provider = next(item for item in options if item.name == provider)
    if not selected_provider.integrated:
        console.print(f"[yellow]{provider} is shown as a placeholder in UI but is not integrated yet.[/yellow]")
        return {"ok": False, "reason": "placeholder_provider", "provider": provider}

    model_options = codex_model_options(paths)
    model_default = model_options[0]
    model = _prompt_or_value(model_input, "Model", default=model_default).strip()
    if model == "<provider-default>":
        model = ""

    sandbox_choices = sandbox_mode_options()
    sandbox_mode = _prompt_or_value(sandbox_input, "Sandbox mode", default=sandbox_choices[0]).strip()
    if sandbox_mode not in sandbox_choices:
        raise typer.BadParameter(f"unsupported sandbox mode '{sandbox_mode}'. choose from: {', '.join(sandbox_choices)}")

    extra_args = _split_extra_args(
        _prompt_or_value(extra_args_input, "Extra provider args (space separated)", default="")
    )

    preview = [
        "autoeval",
        "run",
        "--repo",
        str(repo),
        "--provider",
        provider,
        "--task",
        normalized_task,
        "--mode",
        "planning",
        "--sandbox-mode",
        sandbox_mode,
    ]
    if model:
        preview.extend(["--model", model])
    for value in extra_args:
        preview.extend(["--extra-arg", value])

    table = Table(title="Run Configuration")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Path", selected_path)
    table.add_row("Provider", provider)
    table.add_row("Model", model or "<provider default>")
    table.add_row("Sandbox", sandbox_mode)
    table.add_row("Extra args", " ".join(extra_args) if extra_args else "<none>")
    table.add_row("Task", normalized_task)
    console.print(table)
    console.print("\nCommand preview:\n[bold]" + shlex.join(preview) + "[/bold]")

    if execute:
        console.print("[yellow]Execution from interactive UI is not wired yet; this screen currently prepares validated command input only.[/yellow]")

    return {
        "ok": True,
        "path": selected_path,
        "provider": provider,
        "model": model or None,
        "sandbox_mode": sandbox_mode,
        "extra_args": extra_args,
        "task": normalized_task,
        "execute": execute,
        "command_preview": preview,
    }
