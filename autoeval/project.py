import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path


PROJECT_CONFIG = "autoeval.json"
PROJECT_INSTRUCTIONS = "autoeval.md"
PROJECT_REPORT_INSTRUCTIONS = "report.txt"
APP_ENV = "app.env"
BUILTIN_REPORT_INSTRUCTIONS = Path(__file__).with_name("report.txt")
AUTOEVAL_GIT_DIR = ".autoeval/git"
STAGES = ("script",)
STORAGE_PATHS = ("script", PROJECT_CONFIG, PROJECT_INSTRUCTIONS, ".gitignore")
RUN_ARTIFACT_PATHS = ("output",)
RUN_CONTEXT = {
    "run_dir": "/workspace/run",
    "script_dir": "/workspace/script",
    "app_env_path": "/workspace/app.env",
    "script_params_path": "/workspace/script/params.json",
    "output_dir": "/workspace/run/output",
    "logs_dir": "/workspace/run/logs",
}
INSTRUCTION_FILE = Path(__file__).with_name("instruction.txt")
DOCKER_WARNING = (
    "warning: Docker is not available. Autoeval will still create projects, edit scripts, "
    "run locally, and manage artifacts, but Docker sandboxing is disabled until Docker is installed and usable."
)


def die(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def now():
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def user_data_dir():
    override = os.environ.get("AUTOEVAL_HOME")
    if override:
        return Path(override).expanduser()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "autoeval"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / "autoeval" if base else Path.home() / ".autoeval"

    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "autoeval"


def registry_path():
    return user_data_dir() / "projects.sqlite"


def project_id(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def registry_db():
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        create table if not exists projects(
            project_id text primary key,
            title text not null,
            path text not null unique,
            created_at text not null,
            last_opened_at text not null
        )
        """
    )
    conn.commit()
    return conn


def project_title(root):
    path = Path(root) / PROJECT_CONFIG
    if path.exists():
        return read_json(path).get("title") or Path(root).name
    return Path(root).name


def register_project(root):
    root = Path(root).resolve()
    if not is_project_root(root):
        die(f"{root} is not an autoeval project")

    timestamp = now()
    conn = registry_db()
    conn.execute(
        """
        insert into projects(project_id, title, path, created_at, last_opened_at)
        values (?, ?, ?, ?, ?)
        on conflict(project_id) do update set
            title = excluded.title,
            path = excluded.path,
            last_opened_at = excluded.last_opened_at
        """,
        (project_id(root), project_title(root), str(root), timestamp, timestamp),
    )
    conn.commit()
    conn.close()
    return project_entry(root)


def project_entry(root):
    root = Path(root).resolve()
    return {
        "project_id": project_id(root),
        "title": project_title(root),
        "path": str(root),
        "exists": is_project_root(root),
    }


def list_registered_projects():
    conn = registry_db()
    rows = conn.execute("select * from projects order by last_opened_at desc, title").fetchall()
    conn.close()

    projects = []
    for row in rows:
        root = Path(row["path"])
        projects.append({
            "project_id": row["project_id"],
            "title": project_title(root) if is_project_root(root) else row["title"],
            "path": row["path"],
            "exists": is_project_root(root),
            "last_opened_at": row["last_opened_at"],
        })
    return projects


def resolve_registered_project(project=None):
    projects = list_registered_projects()
    if not projects:
        die("no autoeval projects registered; run `autoeval init <project_name>` first")

    selected = project or next((item["project_id"] for item in projects if item["exists"]), None)
    for item in projects:
        if item["project_id"] == selected:
            if not item["exists"]:
                die(f"registered project is missing or invalid: {item['path']}")
            register_project(item["path"])
            return Path(item["path"])

    die(f"unknown autoeval project: {selected}")


def hash_json(data):
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def gitignore_patterns(root):
    path = root / ".gitignore"
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "!"))
    ]


def ignored(rel, patterns):
    name = Path(rel).name
    parts = Path(rel).parts

    for pattern in patterns:
        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")
        directory = pattern.endswith("/")
        pattern = pattern.rstrip("/")

        if directory:
            if anchored and (rel == pattern or rel.startswith(f"{pattern}/")):
                return True
            if not anchored and pattern in parts:
                return True
        elif fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True

    return False


def storage_paths(root=None):
    root = project_root() if root is None else Path(root)
    paths = list(STORAGE_PATHS)
    config_path = root / PROJECT_CONFIG
    configured = PROJECT_REPORT_INSTRUCTIONS

    if config_path.is_file():
        configured = read_json(config_path).get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS

    report_path = Path(configured)
    if not report_path.is_absolute() and ".." not in report_path.parts and report_path.name:
        paths.insert(-1, report_path.as_posix())

    return tuple(paths)


def hash_dir(path):
    h = hashlib.sha256()
    root = path.parent
    patterns = gitignore_patterns(root)

    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue

        rel = item.relative_to(path).as_posix()
        root_rel = item.relative_to(root).as_posix()
        if ignored(root_rel, patterns):
            continue

        h.update(rel.encode())
        h.update(b"\0")
        h.update(item.read_bytes())
        h.update(b"\0")

    return h.hexdigest()


def is_project_root(path):
    path = Path(path)
    return (path / "script").is_dir() and (path / "runs").is_dir() and all(
        (path / name).is_file()
        for name in (PROJECT_CONFIG, PROJECT_INSTRUCTIONS, ".gitignore")
    )


def project_root():
    for path in (Path.cwd(), *Path.cwd().parents):
        if is_project_root(path):
            return path
    die("not an autoeval project; run `autoeval init <project_name>` first")


def autoeval_git(args, root=None, capture=False, check=True):
    root = project_root() if root is None else Path(root)
    cmd = ["git", "--git-dir", str(root / AUTOEVAL_GIT_DIR), "--work-tree", str(root), *args]

    try:
        proc = subprocess.run(
            cmd,
            check=check,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
        )
    except FileNotFoundError:
        die("required command not found: git")

    return proc.stdout.strip() if capture else None


def require_autoeval_git_repo(root=None):
    root = project_root() if root is None else Path(root)

    if not (root / AUTOEVAL_GIT_DIR).is_dir():
        die(f"{root} is missing its Autoeval git repository")

    top = autoeval_git(["rev-parse", "--show-toplevel"], root=root, capture=True)
    if Path(top).resolve() != root.resolve():
        die("refusing to run git outside the autoeval project")


def script_manifest(root=None):
    root = project_root() if root is None else Path(root)
    path = root / "script" / "stage.json"
    manifest = read_json(path) if path.exists() else die(f"missing {path}")

    for key in ("name", "command", "working_dir", "interface_version"):
        if key not in manifest:
            die(f"{path} missing `{key}`")

    return manifest


def compute_hashes(root=None):
    root = project_root() if root is None else Path(root)
    cfg = read_json(root / PROJECT_CONFIG)
    manifest = script_manifest(root)
    data = {
        "script_hash": hash_dir(root / "script"),
        "script_env_hash": hash_json({"runner": cfg.get("runner", "auto"), "image": manifest.get("image", cfg["sandbox"]["image"])}),
        "runtime_context_hash": hash_json(cfg.get("runtime", {})),
    }
    data["capsule_hash"] = hash_json(data)
    return data


def script_name(run_id, root=None):
    name = script_manifest(root).get("name", "").strip()
    return name if name and name != "script" else f"script-{run_id}"


def app_env_keys(root=None):
    root = project_root() if root is None else Path(root)
    path = root / APP_ENV
    keys = []

    if not path.exists():
        return keys

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())

    return keys


def report_instruction(root=None):
    root = project_root() if root is None else Path(root)
    configured = read_json(root / PROJECT_CONFIG).get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS
    configured_path = Path(configured)
    if configured_path.is_absolute() or ".." in configured_path.parts or not configured_path.name:
        raise ValueError("report instruction file must stay inside the autoeval project")

    path = root / configured_path
    if not path.is_file():
        raise FileNotFoundError(f"missing report instruction file: {configured}")

    return {"source": str(path.relative_to(root)), "text": path.read_text()}


def set_report_instruction(path, root=None):
    root = project_root() if root is None else Path(root)
    path = Path(path)

    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            die("report instruction file must live inside the autoeval project")
    if ".." in path.parts or not path.name:
        die("report instruction file must live inside the autoeval project")

    if not (root / path).is_file():
        die(f"missing report instruction file: {path}")

    cfg = read_json(root / PROJECT_CONFIG)
    cfg["report_instruction_file"] = path.as_posix()
    write_json(root / PROJECT_CONFIG, cfg)
    return path.as_posix()


def write_report_instruction(text, root=None):
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    root = project_root() if root is None else Path(root)
    cfg = read_json(root / PROJECT_CONFIG)
    path = Path(cfg.get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS)

    if path.is_absolute() or ".." in path.parts or not path.name:
        raise ValueError("report instruction file must stay inside the autoeval project")

    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if cfg.get("report_instruction_file") != path.as_posix():
        cfg["report_instruction_file"] = path.as_posix()
        write_json(root / PROJECT_CONFIG, cfg)
    return report_instruction(root)


def artifact_files(base):
    base = Path(base)
    if not base.exists():
        return []

    return [
        {
            "path": item.relative_to(base).as_posix(),
            "text": item.read_text(errors="replace"),
        }
        for item in sorted(base.rglob("*"))
        if item.is_file()
    ]


def write_report_bundle(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run = get_run(run_id, root)
    run_dir = root / (run.get("run_dir") or f"runs/{run_id}")

    if not run_dir.exists():
        die(f"missing run directory: {run_dir.relative_to(root)}")

    source_root = source_root_for_run(run, root)
    params_path = source_root / "script" / "params.json"
    report_dir = run_dir / "report"
    bundle_path = report_dir / "report_bundle.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    bundle = {
        "bundle_path": str(bundle_path.relative_to(root)),
        "run_id": run_id,
        "script": run.get("script_name") or script_name(run_id, source_root),
        "report": run.get("report_path") or "",
        "report_dir": str(report_dir.relative_to(root)),
        "app_env_keys": app_env_keys(root),
        "instruction": report_instruction(root),
        "script_params": read_json(params_path) if params_path.exists() else None,
        "run": {
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "output_hash": run.get("output_hash"),
            "capsule_hash": run.get("capsule_hash"),
        },
        "artifacts": {
            "output": artifact_files(run_dir / "output"),
            "logs": artifact_files(run_dir / "logs"),
        },
    }
    write_json(bundle_path, bundle)
    return bundle


def db(root=None):
    root = project_root() if root is None else Path(root)
    conn = sqlite3.connect(root / "index.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(root=None):
    conn = db(root)
    conn.executescript(
        """
        create table if not exists runs(
            run_id text primary key,
            run_dir text not null,
            report_path text not null,
            output_hash text not null,
            capsule_hash text not null,
            script_name text not null,
            script_hash text not null,
            script_env_hash text not null,
            runtime_context_hash text not null,
            stage_commit text not null,
            unstored_stage_changes integer not null,
            git_commit text not null,
            stored integer not null,
            stored_at text,
            storage_label text,
            status text not null,
            stage_status text not null,
            created_at text not null
        );
        create table if not exists script_versions(
            script_hash text primary key,
            created_at text not null,
            git_commit text not null,
            label text,
            metadata_json text not null
        );
        create index if not exists idx_runs_capsule on runs(capsule_hash, status);
        create index if not exists idx_script_versions_created on script_versions(created_at);
        """
    )
    conn.commit()
    conn.close()


def insert_run(meta, root=None):
    row = {
        "run_dir": f"runs/{meta['run_id']}",
        "report_path": "",
        "output_hash": "",
        "script_name": meta.get("script_name") or script_name(meta["run_id"], root),
        "stored": 0,
        "stored_at": None,
        "storage_label": None,
        **meta,
    }
    row["unstored_stage_changes"] = int(bool(row["unstored_stage_changes"]))
    row["stored"] = int(bool(row["stored"]))
    row["stage_status"] = json.dumps(row["stage_status"])

    conn = db(root)
    conn.execute(
        """
        insert into runs(
            run_id, run_dir, report_path, output_hash, capsule_hash,
            script_name, script_hash, script_env_hash, runtime_context_hash,
            stage_commit, unstored_stage_changes, git_commit,
            stored, stored_at, storage_label, status, stage_status, created_at
        )
        values(
            :run_id, :run_dir, :report_path, :output_hash, :capsule_hash,
            :script_name, :script_hash, :script_env_hash, :runtime_context_hash,
            :stage_commit, :unstored_stage_changes, :git_commit,
            :stored, :stored_at, :storage_label, :status, :stage_status, :created_at
        )
        """,
        row,
    )
    conn.commit()
    conn.close()


def update_run(meta, root=None):
    row = {**meta}
    row["unstored_stage_changes"] = int(bool(row["unstored_stage_changes"]))
    row["stored"] = int(bool(row["stored"]))
    row["stage_status"] = json.dumps(row["stage_status"])

    conn = db(root)
    conn.execute(
        """
        update runs set
            run_dir = :run_dir,
            report_path = :report_path,
            output_hash = :output_hash,
            capsule_hash = :capsule_hash,
            script_name = :script_name,
            script_hash = :script_hash,
            script_env_hash = :script_env_hash,
            runtime_context_hash = :runtime_context_hash,
            stage_commit = :stage_commit,
            unstored_stage_changes = :unstored_stage_changes,
            git_commit = :git_commit,
            stored = :stored,
            stored_at = :stored_at,
            storage_label = :storage_label,
            status = :status,
            stage_status = :stage_status,
            created_at = :created_at
        where run_id = :run_id
        """,
        row,
    )
    conn.commit()
    conn.close()


def upsert_stage_versions(hashes, git_commit, created_at, label=None, root=None, metadata_root=None):
    metadata_root = root if metadata_root is None else metadata_root
    metadata = {"stage": "script", "directory": "script", "manifest": script_manifest(metadata_root)}
    conn = db(root)
    cursor = conn.execute(
        """
        insert into script_versions(script_hash, created_at, git_commit, label, metadata_json)
        values (?, ?, ?, ?, ?)
        on conflict(script_hash) do nothing
        """,
        (
            hashes["script_hash"],
            created_at,
            git_commit,
            label,
            json.dumps(metadata, sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()
    return {"script": cursor.rowcount == 1}


def get_run(run_id, root=None):
    root = project_root() if root is None else Path(root)
    conn = db(root)
    row = conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
    conn.close()

    if row:
        run = dict(row)
        run["stage_status"] = json.loads(run["stage_status"])
        run["unstored_stage_changes"] = bool(run["unstored_stage_changes"])
        run["stored"] = bool(run["stored"])
        return run

    if Path(run_id).name != run_id:
        die(f"unknown run_id: {run_id}")

    path = root / "runs" / run_id / "run.json"
    if path.exists():
        return read_json(path)

    die(f"unknown run_id: {run_id}")


def run_stage_commit(run):
    commit = run.get("stage_commit") or run.get("git_commit")
    if not commit:
        die(f"{run.get('run_id', 'run')} does not record a restorable stage commit")
    return commit


def copy_run_source(src_root, run_root):
    run_root.mkdir(parents=True, exist_ok=True)
    script_target = run_root / "script"
    if script_target.exists():
        shutil.rmtree(script_target)
    shutil.copytree(Path(src_root) / "script", script_target)
    for path in storage_paths(src_root)[1:]:
        target = run_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(src_root) / path, target)


def source_root_for_run(run, root=None):
    root = project_root() if root is None else Path(root)
    run_root = root / (run.get("run_dir") or f"runs/{run['run_id']}")
    if (run_root / "script").is_dir() and (run_root / PROJECT_CONFIG).is_file():
        return run_root
    return root


def hash_path(h, path, base):
    rel = path.relative_to(base).as_posix()
    h.update(rel.encode())
    h.update(b"\0")

    if path.is_dir():
        h.update(b"dir\0")
    else:
        h.update(b"file\0")
        h.update(path.read_bytes())
        h.update(b"\0")


def hash_run_output(run_dir):
    h = hashlib.sha256()
    run_dir = Path(run_dir)

    for label in RUN_ARTIFACT_PATHS:
        path = run_dir / label
        h.update(label.encode())
        h.update(b"\0")

        if not path.exists():
            h.update(b"missing\0")
        elif path.is_file():
            hash_path(h, path, path.parent)
        else:
            h.update(b"dir\0")
            for item in sorted(path.rglob("*")):
                hash_path(h, item, path)

    return h.hexdigest()


def find_duplicate_output_run(hashes, output_hash, root=None):
    root = project_root() if root is None else Path(root)
    conn = db(root)
    rows = conn.execute(
        "select * from runs where capsule_hash = ? and status = 'success' order by created_at desc",
        (hashes["capsule_hash"],),
    ).fetchall()
    conn.close()

    for row in rows:
        run = dict(row)
        run_dir = root / (run.get("run_dir") or f"runs/{run['run_id']}")
        if run_dir.exists() and (run.get("output_hash") or hash_run_output(run_dir)) == output_hash:
            return run

    return None


def run_script(run_dir, root=None, source_root=None):
    root = project_root() if root is None else Path(root)
    source_root = root if source_root is None else Path(source_root)
    cfg = read_json(source_root / PROJECT_CONFIG)
    manifest = script_manifest(source_root)
    workdir = manifest["working_dir"].strip().replace("\\", "/")

    if not workdir.startswith("/workspace/"):
        workdir = f"/workspace/{workdir if workdir.startswith('script') else 'script'}"

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        cfg["sandbox"].get("network", "none"),
        "--cpus",
        str(cfg["sandbox"].get("cpus", "1")),
        "--memory",
        cfg["sandbox"].get("memory", "512m"),
    ]
    app_env = root / APP_ENV
    if app_env.exists():
        cmd += ["--env-file", str(app_env.resolve()), "-v", f"{app_env.resolve()}:/workspace/app.env:ro"]

    cmd += [
        "-e",
        "AUTOEVAL_OUTPUT_DIR=/workspace/run/output",
        "-v",
        f"{(source_root / 'script').resolve()}:/workspace/script:ro",
        "-v",
        f"{run_dir.resolve()}:/workspace/run:rw",
        "-w",
        workdir,
        manifest.get("image", cfg["sandbox"]["image"]),
        "sh",
        "-lc",
        manifest["command"].replace("${CTX}", "/workspace/run/ctx.json"),
    ]

    logs = run_dir / "logs"
    with (logs / "script.stdout.log").open("w") as stdout:
        with (logs / "script.stderr.log").open("w") as stderr:
            return subprocess.run(cmd, stdout=stdout, stderr=stderr).returncode


def docker_ready():
    if shutil.which("docker") is None:
        return False, "required command not found: docker"

    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return False, f"docker is not usable: {exc.stderr.strip() or 'docker daemon is not reachable'}"
    return True, ""


def warn_docker_unavailable():
    ok, message = docker_ready()
    if not ok:
        print(f"{DOCKER_WARNING}\n{message}", file=sys.stderr)
    return ok


def app_env(root):
    path = Path(root) / APP_ENV
    values = {}
    if not path.exists():
        return values

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def local_run_context(run_dir, source_root, root):
    run_dir = Path(run_dir).resolve()
    source_root = Path(source_root).resolve()
    app_env_path = (Path(root) / APP_ENV).resolve()
    return {
        "run_dir": str(run_dir),
        "script_dir": str(source_root / "script"),
        "app_env_path": str(app_env_path),
        "script_params_path": str(source_root / "script" / "params.json"),
        "output_dir": str(run_dir / "output"),
        "logs_dir": str(run_dir / "logs"),
    }


def local_workdir(manifest, run_dir, source_root):
    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if workdir.startswith("/workspace/script"):
        suffix = workdir.removeprefix("/workspace/script").lstrip("/")
        return Path(source_root) / "script" / suffix
    if workdir.startswith("/workspace/run"):
        suffix = workdir.removeprefix("/workspace/run").lstrip("/")
        return Path(run_dir) / suffix
    if workdir.startswith("script"):
        return Path(source_root) / workdir
    return Path(source_root) / "script"


def run_script_local(run_dir, root=None, source_root=None):
    root = project_root() if root is None else Path(root)
    source_root = root if source_root is None else Path(source_root)
    manifest = script_manifest(source_root)
    command = manifest["command"].replace("${CTX}", shlex.quote(str((Path(run_dir) / "ctx.json").resolve())))
    if command.startswith("python "):
        command = f"{shlex.quote(sys.executable)} {command.removeprefix('python ')}"
    elif command.startswith("python3 "):
        command = f"{shlex.quote(sys.executable)} {command.removeprefix('python3 ')}"
    env = os.environ.copy()
    env.update(app_env(root))
    env.update({
        "AUTOEVAL_RUN_DIR": str(Path(run_dir).resolve()),
        "AUTOEVAL_SCRIPT_DIR": str((Path(source_root) / "script").resolve()),
        "AUTOEVAL_OUTPUT_DIR": str((Path(run_dir) / "output").resolve()),
    })
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout:
        with (logs / "script.stderr.log").open("w") as stderr:
            return subprocess.run(
                command,
                cwd=local_workdir(manifest, run_dir, source_root),
                env=env,
                shell=True,
                stdout=stdout,
                stderr=stderr,
            ).returncode


def runner_type(root):
    cfg = read_json(Path(root) / PROJECT_CONFIG)
    requested = cfg.get("runner", "auto")
    if requested not in {"auto", "docker", "local"}:
        die("autoeval.json runner must be one of: auto, docker, local")
    if requested == "local":
        return "local"
    ok, message = docker_ready()
    if requested == "docker":
        if not ok:
            die(message)
        return "docker"
    if ok:
        return "docker"
    print(f"{DOCKER_WARNING}\n{message}", file=sys.stderr)
    return "local"


def current_autoeval_commit(root=None):
    return autoeval_git(["rev-parse", "HEAD"], root=root, capture=True)


def git_status(paths, root=None):
    return autoeval_git(["status", "--porcelain", "--", *paths], root=root, capture=True)


def git_commit_storage(message, root=None):
    paths = storage_paths(root)
    autoeval_git(["add", *paths], root=root)

    if not autoeval_git(["diff", "--cached", "--name-only", "--", *paths], root=root, capture=True):
        return current_autoeval_commit(root), False

    autoeval_git(["commit", "-m", message, "--", *paths], root=root)
    return current_autoeval_commit(root), True


def git_commit_run(run_id, root=None):
    root = project_root() if root is None else Path(root)
    paths = [f"runs/{run_id}"]

    autoeval_git(["add", "-f", *paths], root=root)

    if not autoeval_git(["diff", "--cached", "--name-only", "--", *paths], root=root, capture=True):
        return current_autoeval_commit(root), False

    autoeval_git(["commit", "-m", f"autoeval run {run_id}", "--only", "--", *paths], root=root)
    commit = current_autoeval_commit(root)
    autoeval_git(["tag", "-f", f"run-{run_id}", commit], root=root)
    return commit, True


def restore_run_state(run_id, root=None):
    root = project_root() if root is None else Path(root)
    require_autoeval_git_repo(root)

    if git_status(storage_paths(root), root=root):
        die("refusing to restore run state over unstored script/config changes")

    run = get_run(run_id, root)
    source_root = source_root_for_run(run, root)

    if source_root != root:
        copy_run_source(source_root, root)
    else:
        autoeval_git(["checkout", run_stage_commit(run), "--", *storage_paths(root)], root=root)

    if run.get("unstored_stage_changes"):
        print("note: restored a run that used unstored script/config changes.", file=sys.stderr)

    return run, run_stage_commit(run)


DEFAULT_SCRIPT = """import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
ctx = json.loads(Path(parser.parse_args().ctx).read_text())
params = json.loads(Path(ctx["script_params_path"]).read_text())
result = {
    "message": os.environ.get("AUTOEVAL_MESSAGE", params["message"]),
    "source": "app.env" if "AUTOEVAL_MESSAGE" in os.environ else "script params",
}

Path(ctx["output_dir"]).mkdir(parents=True, exist_ok=True)
(Path(ctx["output_dir"]) / "result.json").write_text(json.dumps(result, indent=2) + "\\n")
"""


def write_default_project(root, title):
    for name in ("script", "runs", ".autoeval"):
        (root / name).mkdir(parents=True)

    write_json(
        root / PROJECT_CONFIG,
        {"title": title, "description": "", "runner": "auto", "sandbox": {"image": "python:3.12-slim", "network": "none", "cpus": "1", "memory": "512m"}, "runtime": {}, "report_instruction_file": PROJECT_REPORT_INSTRUCTIONS},
    )
    write_json(root / "script" / "stage.json", {"name": "script", "command": "python script.py --ctx ${CTX}", "working_dir": "script", "interface_version": "1"})
    write_json(root / "script" / "params.json", {"message": "hello from script params"})
    write_json(
        root / "script" / "params.schema.json",
        {"type": "object", "properties": {"message": {"type": "string", "title": "Message", "default": "hello from script params"}}, "required": ["message"]},
    )
    (root / "script" / "script.py").write_text(DEFAULT_SCRIPT)
    (root / PROJECT_INSTRUCTIONS).write_text(INSTRUCTION_FILE.read_text())
    (root / PROJECT_REPORT_INSTRUCTIONS).write_text(BUILTIN_REPORT_INSTRUCTIONS.read_text())
    (root / APP_ENV).write_text(
        "# Project-local environment for Autoeval runs.\n# Values here are passed to the runner and are not stored by Autoeval.\nAUTOEVAL_MESSAGE=hello from app.env\n"
    )
    (root / ".gitignore").write_text(
        "/.autoeval/\n/app.env\n/index.sqlite\n/runs/\n/server/\n__pycache__/\n*.pyc\n"
    )


def create_project(root, title):
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        die(f"{root} already exists and is not empty")

    root.mkdir(parents=True, exist_ok=True)
    write_default_project(root, title)
    autoeval_git(["init", "-b", "main"], root=root)
    require_autoeval_git_repo(root)

    if not autoeval_git(["config", "user.name"], root=root, capture=True, check=False):
        autoeval_git(["config", "user.name", "Autoeval"], root=root)
    if not autoeval_git(["config", "user.email"], root=root, capture=True, check=False):
        autoeval_git(["config", "user.email", "autoeval@local"], root=root)

    init_db(root)
    autoeval_git(["add", *storage_paths(root)], root=root)
    autoeval_git(["commit", "-m", "autoeval init"], root=root)
    return root


def new_run_id(hashes, root):
    created_at = now()
    while True:
        run_id = f"{created_at}_{hashes['capsule_hash'][:8]}_{uuid.uuid4().hex[:6]}"
        if not (root / "runs" / run_id).exists():
            return run_id, created_at
