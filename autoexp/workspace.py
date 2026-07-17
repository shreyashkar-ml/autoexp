"""Repository identity, experiment context, and global Autoexp paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path


# These files exist only inside immutable snapshots and global run directories.
# Autoexp never writes this directory into the user's repository.
AUTOEXP_DIR = ".autoexp"
PROJECT_CONFIG = f"{AUTOEXP_DIR}/project.json"
PROJECT_REPORT_INSTRUCTIONS = f"{AUTOEXP_DIR}/report-instructions.md"
PROJECT_REPORT = "reports/report.md"
STAGE_MANIFEST = f"{AUTOEXP_DIR}/stage.json"
PARAMS_FILE = f"{AUTOEXP_DIR}/params.json"
PARAMS_SCHEMA_FILE = f"{AUTOEXP_DIR}/params.schema.json"
APP_ENV = ".env"
BUILTIN_REPORT_INSTRUCTIONS = Path(__file__).with_name("report.txt")

FILE_ROLES = {
    "entrypoint",
    "editable-source",
    "supporting-source",
    "frozen-evaluator",
    "input-data",
    "generated-output",
    "report-guidance",
    "secret-source",
}
SNAPSHOT_EXCLUDED_ROLES = {"generated-output", "secret-source"}


def die(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def now():
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def user_data_dir():
    override = os.environ.get("AUTOEXP_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "autoexp"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / "autoexp" if base else Path.home() / ".autoexp"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "autoexp"


def repo_data_dir(repo_id):
    return user_data_dir() / "repos" / repo_id


def experiment_data_dir(repo_id, experiment_id):
    return repo_data_dir(repo_id) / "experiments" / experiment_id


def is_within_project(path):
    path = Path(path)
    return not path.is_absolute() and ".." not in path.parts and bool(path.name)


def ensure_within_project(path, message):
    if not is_within_project(path):
        raise ValueError(message)
    return Path(path)


def _git_root(path):
    path = Path(path).expanduser().resolve()
    cwd = path if path.is_dir() else path.parent
    try:
        value = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"not inside a Git worktree: {path}") from exc
    return Path(value).resolve()


def project_id(root):
    """Stable identity for one canonical Git worktree path."""
    try:
        return _context(root)["repo_id"]
    except ValueError:
        path = _git_root(root)
        return hashlib.sha256(str(path).encode()).hexdigest()[:16]


def repository_root(root=None):
    if root is None:
        return _git_root(Path.cwd())
    path = Path(root).expanduser()
    try:
        return Path(_context(path)["repo_path"])
    except ValueError:
        return _git_root(path)


def _context(root):
    """Resolve an experiment data directory/id to its global database row."""
    from .store import db

    candidate = Path(str(root)).expanduser()
    raw = str(candidate.resolve()) if candidate.exists() else str(root)
    conn = db()
    row = conn.execute(
        """select e.*, r.path as repo_path, r.title as repo_title
           from experiments e join repositories r on r.repo_id = e.repo_id
           where e.experiment_id = ? or e.data_path = ?""",
        (raw, raw),
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"unknown Autoexp experiment: {root}")
    return dict(row)


def register_repository(path=None, *, title=None):
    from .store import db

    root = _git_root(path or Path.cwd())
    timestamp = now()
    conn = db()
    existing = conn.execute(
        "select repo_id, title from repositories where path = ?", (str(root),)
    ).fetchone()
    repo_id = (
        existing["repo_id"]
        if existing
        else hashlib.sha256(str(root).encode()).hexdigest()[:16]
    )
    display = title or (existing["title"] if existing else root.name)
    conn.execute(
        """insert into repositories(repo_id, title, path, created_at, last_opened_at)
           values (?, ?, ?, ?, ?)
           on conflict(repo_id) do update set
             title = coalesce(nullif(excluded.title, ''), repositories.title),
             path = excluded.path,
             last_opened_at = excluded.last_opened_at""",
        (repo_id, display, str(root), timestamp, timestamp),
    )
    conn.commit()
    conn.close()
    repo_data_dir(repo_id).mkdir(parents=True, exist_ok=True)
    return {"repo_id": repo_id, "title": display, "path": str(root)}



def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:36] or "experiment"


def create_experiment(
    objective,
    *,
    root=None,
    title=None,
    kind="standard",
    entrypoint=None,
    command=None,
    working_dir=None,
    runner="local",
    config=None,
):
    from .store import db, init_db

    if kind not in {"standard", "autoresearch"}:
        raise ValueError("kind must be standard or autoresearch")
    if runner not in {"local", "docker"}:
        raise ValueError("runner must be local or docker")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("experiment objective is required")

    repo = register_repository(root)
    display = (title or objective.strip().splitlines()[0])[:120]
    experiment_id = f"exp_{_slug(display)}_{uuid.uuid4().hex[:6]}"
    data_path = experiment_data_dir(repo["repo_id"], experiment_id)
    stage = {
        "name": Path(entrypoint).name if entrypoint else "",
        "command": command or (f"python {shlex.quote(Path(entrypoint).name)} --ctx ${{CTX}}" if entrypoint else ""),
        "working_dir": working_dir or (Path(entrypoint).parent.as_posix() if entrypoint else "."),
        "interface_version": "1",
    }
    settings = {
        "runner": runner,
        "sandbox": {
            "image": "python:3.12-slim",
            "network": "none",
            "cpus": "1",
            "memory": "512m",
        },
        "runtime": {},
        "external_inputs": [],
        **(config or {}),
    }
    timestamp = now()
    conn = db()
    conn.execute(
        """insert into experiments(
             experiment_id, repo_id, title, objective, kind, status, runner,
             stage, config, params, params_schema, report_guidance, data_path,
             created_at, updated_at
           ) values (?, ?, ?, ?, ?, 'active', ?, ?, ?, '{}', ?, ?, ?, ?, ?)""",
        (
            experiment_id,
            repo["repo_id"],
            display,
            objective.strip(),
            kind,
            runner,
            json.dumps(stage, sort_keys=True),
            json.dumps(settings, sort_keys=True),
            json.dumps({"type": "object", "properties": {}}, sort_keys=True),
            BUILTIN_REPORT_INSTRUCTIONS.read_text(),
            str(data_path),
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    conn.close()
    for name in ("runs", "reports", "insights"):
        (data_path / name).mkdir(parents=True, exist_ok=True)
    if entrypoint:
        declare_file(experiment_id, entrypoint, "entrypoint")
    init_db(data_path)
    return experiment_entry(experiment_id)


def experiment_id(root=None):
    return _context(resolve_root(root))["experiment_id"]


def experiment_entry(root):
    context = _context(root)
    context["stage"] = json.loads(context["stage"])
    context["config"] = json.loads(context["config"])
    context["params"] = json.loads(context["params"])
    context["params_schema"] = json.loads(context["params_schema"])
    context["exists"] = Path(context["repo_path"]).is_dir()
    context["mode"] = context["kind"]
    context["project_id"] = context["experiment_id"]
    context["path"] = context["repo_path"]
    return context


def project_entry(root):
    return experiment_entry(resolve_root(root))


def list_experiments(repo_id=None):
    from .store import db

    conn = db()
    sql = "select experiment_id from experiments"
    args = ()
    if repo_id:
        sql += " where repo_id = ?"
        args = (repo_id,)
    sql += " order by updated_at desc, title"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [experiment_entry(row["experiment_id"]) for row in rows]




def registry():
    from .store import db

    conn = db()
    repos = [dict(row) for row in conn.execute(
        "select * from repositories order by last_opened_at desc, title"
    ).fetchall()]
    conn.close()
    experiments = list_experiments()
    by_repo = {}
    for item in experiments:
        by_repo.setdefault(item["repo_id"], []).append(item)
    for repo in repos:
        repo["exists"] = Path(repo["path"]).is_dir()
        repo["experiments"] = by_repo.get(repo["repo_id"], [])
    return repos


def resolve_root(root=None, experiment=None):
    """Return the global data root for one active experiment."""
    if root is not None:
        try:
            return Path(_context(root)["data_path"])
        except ValueError:
            pass
    wanted = experiment or os.environ.get("AUTOEXP_EXPERIMENT")
    if wanted:
        context = _context(wanted)
        if root is not None and repository_root(root) != Path(context["repo_path"]):
            raise ValueError(f"experiment {wanted} belongs to another repository")
        return Path(context["data_path"])

    repo = register_repository(root or Path.cwd())
    items = list_experiments(repo["repo_id"])
    if not items:
        raise ValueError(
            "this repository has no Autoexp experiment; run "
            "`autoexp experiment create \"<objective>\"`"
        )
    return Path(items[0]["data_path"])




def project_mode(root):
    return _context(resolve_root(root))["kind"]


def _safe_repo_path(repo_root, rel):
    rel = ensure_within_project(rel, "file path must stay inside the repository")
    target = repo_root / rel
    cursor = repo_root
    for part in rel.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"declared files must not contain symlinks: {rel}")
    if not target.resolve(strict=False).is_relative_to(repo_root.resolve()):
        raise ValueError("file path must stay inside the repository")
    return target


def safe_repository_path(root, rel):
    return _safe_repo_path(repository_root(root), rel)


def _secret_keys(path):
    if not path.is_file():
        return []
    keys = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            keys.append({"name": key.strip(), "populated": bool(value.strip())})
    return keys


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def declare_file(root, path, role, *, description=""):
    from .store import db

    if role not in FILE_ROLES:
        raise ValueError(f"role must be one of: {', '.join(sorted(FILE_ROLES))}")
    context = _context(resolve_root(root))
    rel = ensure_within_project(path, "file path must stay inside the repository").as_posix()
    target = _safe_repo_path(Path(context["repo_path"]), rel)
    secret_keys = _secret_keys(target) if role == "secret-source" else []
    digest = None if role == "secret-source" else _file_hash(target)
    conn = db()
    conn.execute(
        """insert into manifest_files(
             experiment_id, path, role, description, content_hash, available,
             secret_keys, updated_at
           ) values (?, ?, ?, ?, ?, ?, ?, ?)
           on conflict(experiment_id, path) do update set
             role = excluded.role, description = excluded.description,
             content_hash = excluded.content_hash, available = excluded.available,
             secret_keys = excluded.secret_keys, updated_at = excluded.updated_at""",
        (
            context["experiment_id"],
            rel,
            role,
            description,
            digest,
            int(target.is_file()),
            json.dumps(secret_keys),
            now(),
        ),
    )
    if role == "entrypoint":
        stage = json.loads(context["stage"])
        stage["name"] = rel
        if not stage.get("command"):
            stage["command"] = f"python {Path(rel).name} --ctx ${{CTX}}"
            stage["working_dir"] = Path(rel).parent.as_posix()
        conn.execute(
            "update experiments set stage = ?, updated_at = ? where experiment_id = ?",
            (json.dumps(stage, sort_keys=True), now(), context["experiment_id"]),
        )
    conn.commit()
    conn.close()
    return next(item for item in manifest_files(root) if item["path"] == rel)


def manifest_files(root=None, *, refresh=True):
    from .store import db

    context = _context(resolve_root(root))
    repo_root = Path(context["repo_path"])
    conn = db()
    rows = conn.execute(
        "select * from manifest_files where experiment_id = ? order by path",
        (context["experiment_id"],),
    ).fetchall()
    items = []
    for raw in rows:
        item = dict(raw)
        target = _safe_repo_path(repo_root, item["path"])
        if refresh:
            keys = _secret_keys(target) if item["role"] == "secret-source" else []
            digest = None if item["role"] == "secret-source" else _file_hash(target)
            available = int(target.is_file())
            conn.execute(
                """update manifest_files set content_hash = ?, available = ?,
                   secret_keys = ?, updated_at = ?
                   where experiment_id = ? and path = ?""",
                (
                    digest,
                    available,
                    json.dumps(keys),
                    now(),
                    context["experiment_id"],
                    item["path"],
                ),
            )
            item.update(content_hash=digest, available=available, secret_keys=json.dumps(keys))
        item["available"] = bool(item["available"])
        item["secret_keys"] = json.loads(item.get("secret_keys") or "[]")
        items.append(item)
    conn.commit()
    conn.close()
    return items


def experiment_config(root=None):
    root = Path(root) if root is not None else resolve_root()
    control = root / PROJECT_CONFIG
    if control.is_file():
        return read_json(control)
    entry = experiment_entry(resolve_root(root))
    config = entry["config"]
    files = manifest_files(root)
    config.update(
        {
            "title": entry["title"],
            "objective": entry["objective"],
            "kind": entry["kind"],
            "mode": entry["kind"],
            "runner": entry["runner"],
            "stage": entry["stage"],
            "params": entry["params"],
            "params_schema": entry["params_schema"],
            "report_guidance": entry["report_guidance"],
            "files": files,
            "source": {
                "editable": [
                    item["path"]
                    for item in files
                    if item["role"] in {"entrypoint", "editable-source"}
                ]
            },
        }
    )
    return config


def script_manifest(root=None):
    root = Path(root) if root is not None else resolve_root()
    path = root / STAGE_MANIFEST
    manifest = read_json(path) if path.is_file() else experiment_config(root)["stage"]
    for key in ("name", "command", "working_dir", "interface_version"):
        if key not in manifest:
            raise ValueError(f"experiment stage is missing `{key}`")
    return manifest


def source_paths(root=None):
    root = Path(root) if root is not None else resolve_root()
    config_path = root / PROJECT_CONFIG
    files = experiment_config(root).get("files", [])
    paths = [
        item["path"]
        for item in files
        if item.get("role") not in SNAPSHOT_EXCLUDED_ROLES
    ]
    if config_path.is_file():
        paths.extend(
            (
                PROJECT_CONFIG,
                STAGE_MANIFEST,
                PARAMS_FILE,
                PARAMS_SCHEMA_FILE,
                PROJECT_REPORT_INSTRUCTIONS,
            )
        )
    return tuple(dict.fromkeys(paths))


def materialize_workspace(root, destination):
    """Copy declared non-secret files and generated controls into a snapshot tree."""
    context_root = resolve_root(root)
    context = experiment_entry(context_root)
    repo_root = Path(context["repo_path"])
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("snapshot destination must be empty")

    files = manifest_files(context_root)
    for item in files:
        if item["role"] in SNAPSHOT_EXCLUDED_ROLES or not item["available"]:
            continue
        source = _safe_repo_path(repo_root, item["path"])
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    config = experiment_config(context_root)
    write_json(destination / PROJECT_CONFIG, config)
    write_json(destination / STAGE_MANIFEST, context["stage"])
    write_json(destination / PARAMS_FILE, context["params"])
    write_json(destination / PARAMS_SCHEMA_FILE, context["params_schema"])
    (destination / PROJECT_REPORT_INSTRUCTIONS).parent.mkdir(parents=True, exist_ok=True)
    (destination / PROJECT_REPORT_INSTRUCTIONS).write_text(context["report_guidance"])
    return destination


def run_dir_for(run, root):
    root = resolve_root(root)
    runs_root = (root / "runs").resolve()
    raw = Path(run.get("run_dir") or f"runs/{run['run_id']}")
    path = (root / raw).resolve()
    if raw.is_absolute() or ".." in raw.parts or not path.is_relative_to(runs_root):
        raise ValueError("run directory must stay inside global Autoexp storage")
    return path
