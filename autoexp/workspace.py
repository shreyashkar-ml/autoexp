import fnmatch
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


PROJECT_CONFIG = "autoexp.json"
PROJECT_INSTRUCTIONS = "autoexp.md"
PROJECT_REPORT_INSTRUCTIONS = "report.txt"
APP_ENV = "app.env"
SOURCE_PATHS = ("script", PROJECT_CONFIG, PROJECT_INSTRUCTIONS, ".gitignore")
INSTRUCTION_FILE = Path(__file__).with_name("instruction.txt")
BUILTIN_REPORT_INSTRUCTIONS = Path(__file__).with_name("report.txt")
AGENTS_TEXT = """# Autoexp Workspace

This repository is an Autoexp workspace.

- Read `autoexp.md` before changing experiment behavior.
- When MCP tools are available, use their live schemas. Start with `workspace` and `contract`; standard work uses `list_runs`, `read_*`, `write_*`, `run`, `diff_runs`, and `restore_run`; Autoresearch uses `research_state`, `research_begin_attempt`, `research_finish_attempt`, and `research_diff`.
- MCP tool names are not shell commands. Without MCP, use the `autoexp` CLI and project files; do not run `autoexp mcp` manually.
- In Autoresearch projects, `script/train.py` is the baseline implementation. If the user provides a reference training script, adapt or copy it into `script/train.py` before the first attempt.
- In Autoresearch projects, call `research_preflight` before the loop, edit only the configured agent-owned candidate file, and finish every begun attempt through Autoexp. The frozen evaluator is read-only within a contract; reverted candidate snapshots and runs remain evidence.
- Keep experiment source in `script/`.
- Do not create ad-hoc experiment folders.
- Do not hand-edit `runs/<run_id>/output/` or `runs/<run_id>/logs/`.
- Generated reports belong under `runs/<run_id>/report/`.
"""
DEFAULT_SCRIPT = """import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
ctx = json.loads(Path(parser.parse_args().ctx).read_text())
params = json.loads(Path(ctx["script_params_path"]).read_text())
result = {
    "message": os.environ.get("AUTOEXP_MESSAGE", params["message"]),
    "source": "app.env" if "AUTOEXP_MESSAGE" in os.environ else "script params",
}

Path(ctx["output_dir"]).mkdir(parents=True, exist_ok=True)
(Path(ctx["output_dir"]) / "result.json").write_text(json.dumps(result, indent=2) + "\\n")
"""


# ======================================================================
#  Small primitives
# ======================================================================

def die(message):
    """Print a user-facing error and stop the program."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def now():
    """UTC timestamp in a filename-safe ISO-like form."""
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


# ======================================================================
#  Project location and path safety
# ======================================================================

def is_project_root(path):
    """True when path has the directory/file shape Autoexp expects of a project."""
    path = Path(path)
    has_dirs = (path / "script").is_dir() and (path / "runs").is_dir()
    has_files = all(
        (path / name).is_file()
        for name in (PROJECT_CONFIG, PROJECT_INSTRUCTIONS, ".gitignore")
    )
    return has_dirs and has_files


def project_root():
    """Walk up from the current directory to the nearest project root."""
    for path in (Path.cwd(), *Path.cwd().parents):
        if is_project_root(path):
            return path
    die("not an autoexp project; run `autoexp init <project_name>` first")


def resolve_root(root=None):
    """The active project root: the caller's path, or the discovered one."""
    return project_root() if root is None else Path(root)


def run_dir_for(run, root):
    """On-disk location of a run, honoring an explicit run_dir or the default layout."""
    root = Path(root).resolve()
    runs_root = (root / "runs").resolve()
    raw = Path(run.get("run_dir") or f"runs/{run['run_id']}")
    path = (root / raw).resolve()
    if (
        raw.is_absolute()
        or ".." in raw.parts
        or len(raw.parts) < 2
        or raw.parts[0] != "runs"
        or not runs_root.is_relative_to(root)
        or not path.is_relative_to(runs_root)
    ):
        raise ValueError("run directory must stay inside the project's runs directory")
    return path


def is_within_project(path):
    """True when path is relative, has a name, and cannot escape via '..'."""
    path = Path(path)
    return not path.is_absolute() and ".." not in path.parts and bool(path.name)


def ensure_within_project(path, message):
    """Return path as a Path, raising ValueError if it would escape the project."""
    if not is_within_project(path):
        raise ValueError(message)
    return Path(path)


# ======================================================================
#  Per-user project registry
# ======================================================================

def user_data_dir():
    override = os.environ.get("AUTOEXP_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "autoexp"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / "autoexp" if base else Path.home() / ".autoexp"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "autoexp"


def project_id(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def registry_db():
    path = user_data_dir() / "projects.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """create table if not exists projects(
            project_id text primary key, title text not null, path text not null unique,
            created_at text not null, last_opened_at text not null
        )"""
    )
    conn.commit()
    return conn


def project_mode(root):
    path = Path(root) / PROJECT_CONFIG
    if not path.exists():
        return "standard"
    return read_json(path).get("mode", "standard")


def project_entry(root):
    root = Path(root).resolve()
    config_path = root / PROJECT_CONFIG
    config = read_json(config_path) if config_path.exists() else {}
    return {
        "project_id": project_id(root),
        "title": config.get("title") or root.name,
        "path": str(root),
        "exists": is_project_root(root),
        "runner": config.get("runner", "local"),
        "mode": config.get("mode", "standard"),
    }


def register_project(root):
    root = Path(root).resolve()
    if not is_project_root(root):
        die(f"{root} is not an autoexp project")
    timestamp = now()
    entry = project_entry(root)
    conn = registry_db()
    conn.execute(
        """insert into projects(project_id, title, path, created_at, last_opened_at)
        values (?, ?, ?, ?, ?)
        on conflict(project_id) do update set title = excluded.title, path = excluded.path,
        last_opened_at = excluded.last_opened_at""",
        (entry["project_id"], entry["title"], str(root), timestamp, timestamp),
    )
    conn.commit()
    conn.close()
    return entry


def list_registered_projects():
    conn = registry_db()
    rows = conn.execute("select * from projects order by last_opened_at desc, title").fetchall()
    conn.close()
    projects = []
    for row in rows:
        path = Path(row["path"])
        entry = project_entry(path)
        if not entry["exists"]:
            entry.update(project_id=row["project_id"], title=row["title"], runner="local", mode="standard")
        projects.append({**entry, "last_opened_at": row["last_opened_at"]})
    return projects


def resolve_registered_project(project=None):
    projects = list_registered_projects()
    if not projects:
        die("no autoexp projects registered; run `autoexp init <project_name>` first")
    selected = project or next((item["project_id"] for item in projects if item["exists"]), None)
    for item in projects:
        if item["project_id"] == selected:
            if not item["exists"]:
                die(f"registered project is missing or invalid: {item['path']}")
            register_project(item["path"])
            return Path(item["path"])
    die(f"unknown autoexp project: {selected}")


# ======================================================================
#  .gitignore matching
# ======================================================================

def gitignore_patterns(root):
    path = Path(root) / ".gitignore"
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "!"))
    ]


def ignored(rel, patterns):
    """True when project-relative path `rel` matches any .gitignore pattern."""
    name = Path(rel).name
    parts = Path(rel).parts
    for raw in patterns:
        anchored = raw.startswith("/")
        pattern = raw.lstrip("/")
        is_dir_pattern = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if is_dir_pattern:
            if anchored and (rel == pattern or rel.startswith(f"{pattern}/")):
                return True
            if not anchored and pattern in parts:
                return True
        elif fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


# ======================================================================
#  Source set and script manifest
# ======================================================================

def source_paths(root=None):
    """The tracked source set: script/, config, contract, .gitignore, and report instruction."""
    root = resolve_root(root)
    paths = list(SOURCE_PATHS)
    configured = read_json(root / PROJECT_CONFIG).get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS
    report_path = Path(configured)
    if is_within_project(report_path):
        paths.insert(-1, report_path.as_posix())
    return tuple(paths)


def script_manifest(root=None):
    """Read and validate script/stage.json."""
    root = resolve_root(root)
    path = root / "script" / "stage.json"
    if not path.exists():
        die(f"missing {path}")
    manifest = read_json(path)
    for key in ("name", "command", "working_dir", "interface_version"):
        if key not in manifest:
            die(f"{path} missing `{key}`")
    return manifest


# ======================================================================
#  Project scaffolding
# ======================================================================

def write_default_project(root, title, runner, autoresearch=False):
    for name in ("script", "runs", ".autoexp"):
        (root / name).mkdir(parents=True)

    config = {
        "title": title,
        "description": "",
        "runner": runner,
        "sandbox": {"image": "python:3.12-slim", "network": "none", "cpus": "1", "memory": "512m"},
        "runtime": {},
        "report_instruction_file": PROJECT_REPORT_INSTRUCTIONS,
    }
    if autoresearch:
        from .autoresearch import config_block, scaffold

        config.update(config_block())
    write_json(root / PROJECT_CONFIG, config)
    if autoresearch:
        scaffold(root, write_json)
    else:
        write_json(root / "script" / "stage.json", {
            "name": "script",
            "command": "python script.py --ctx ${CTX}",
            "working_dir": "script",
            "interface_version": "1",
        })
        write_json(root / "script" / "params.json", {"message": "hello from script params"})
        write_json(root / "script" / "params.schema.json", {
            "type": "object",
            "properties": {"message": {"type": "string", "title": "Message", "default": "hello from script params"}},
            "required": ["message"],
        })
        (root / "script" / "script.py").write_text(DEFAULT_SCRIPT)
    (root / PROJECT_INSTRUCTIONS).write_text(INSTRUCTION_FILE.read_text())
    (root / PROJECT_REPORT_INSTRUCTIONS).write_text(BUILTIN_REPORT_INSTRUCTIONS.read_text())
    (root / "AGENTS.md").write_text(AGENTS_TEXT)
    (root / "CLAUDE.md").write_text(AGENTS_TEXT)
    write_json(root / ".mcp.json", {"mcpServers": {"autoexp": {"command": "autoexp", "args": ["mcp"]}}})
    (root / APP_ENV).write_text(
        "# Project-local environment for Autoexp runs.\n"
        "# Values here are passed to the runner and remain outside Autoexp history.\n"
        "AUTOEXP_MESSAGE=hello from app.env\n"
    )
    (root / ".gitignore").write_text(
        "/.autoexp/\n/app.env\n/index.sqlite\n/runs/\n/server/\n__pycache__/\n*.pyc\n"
    )


def create_project(root, title, runner="local", autoresearch=False):
    from .store import AUTOEXP_GIT_DIR, autoexp_git, init_db, require_autoexp_git_repo

    root = Path(root)
    if root.exists() and any(root.iterdir()):
        die(f"{root} already exists and is not empty")
    root.mkdir(parents=True, exist_ok=True)
    write_default_project(root, title, runner, autoresearch)
    autoexp_git(["init", "-b", "main"], root=root)
    require_autoexp_git_repo(root)
    (root / AUTOEXP_GIT_DIR / "info" / "exclude").write_text("/.mcp.json\n/AGENTS.md\n/CLAUDE.md\n")
    for key, value in (("user.name", "Autoexp"), ("user.email", "autoexp@local")):
        if not autoexp_git(["config", key], root=root, capture=True, check=False):
            autoexp_git(["config", key, value], root=root)
    init_db(root)
    autoexp_git(["add", *source_paths(root)], root=root)
    autoexp_git(["commit", "-m", "autoexp init"], root=root)
    return root
