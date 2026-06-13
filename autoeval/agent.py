import json
from pathlib import Path

from .project import project_root


AGENTS_TEXT = """# Autoeval Workspace

This repository is an Autoeval workspace.

- Read `autoeval.md` before changing experiment behavior.
- Use `autoeval run`, `autoeval storage`, and Autoeval MCP tools for runs and artifact inspection.
- Keep experiment source in `script/`.
- Do not create ad-hoc experiment folders.
- Do not hand-edit `runs/<run_id>/output/` or `runs/<run_id>/logs/`.
- Generated reports belong under `runs/<run_id>/report/`.
"""

CLAUDE_TEXT = AGENTS_TEXT


def mcp_config():
    return {
        "mcpServers": {
            "autoeval": {
                "command": "autoeval",
                "args": ["mcp"],
            }
        }
    }


def write_if_allowed(path, text, force=False):
    if path.exists() and not force:
        raise FileExistsError(f"{path.name} already exists; use --force to overwrite")
    path.write_text(text)
    return path.name


def install_agent_files(target="all", force=False, root=None):
    root = project_root() if root is None else Path(root)
    if target not in {"codex", "claude", "all"}:
        raise ValueError("target must be codex, claude, or all")

    written = []
    if target in {"codex", "all"}:
        written.append(write_if_allowed(root / "AGENTS.md", AGENTS_TEXT, force))
    if target in {"claude", "all"}:
        written.append(write_if_allowed(root / "CLAUDE.md", CLAUDE_TEXT, force))
    written.append(write_if_allowed(root / ".mcp.json", json.dumps(mcp_config(), indent=2) + "\n", force))
    return {"target": target, "written": written}
