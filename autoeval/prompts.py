from __future__ import annotations

from pathlib import Path


PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    prompt_path = PROMPTS_DIR / f"{name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def load_decision_prompt() -> str:
    return load_prompt("decision")


def load_github_agent_prompt() -> str:
    return load_prompt("github_agent_prompt")


def load_slack_agent_prompt() -> str:
    return load_prompt("slack_agent_prompt")
