import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path


PROJECT_CONFIG = "autoeval.json"
PROJECT_INSTRUCTIONS = "autoeval.md"
STAGES = ("input", "script", "report")
STAGE_HASH_KEYS = {
    "input": "input_hash",
    "script": "script_hash",
    "report": "report_hash",
}
STAGE_VERSION_TABLES = {
    "input": "input_versions",
    "script": "script_versions",
    "report": "report_versions",
}
STORAGE_PATHS = (
    "input",
    "script",
    "report",
    PROJECT_CONFIG,
    PROJECT_INSTRUCTIONS,
    ".gitignore",
)
DEFAULT_INPUT_PARAMS = {
    "message": "hello from input",
}
DEFAULT_INPUT_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "title": "Message",
            "default": DEFAULT_INPUT_PARAMS["message"],
        },
    },
    "required": ["message"],
}

WORKSPACE_INSTRUCTIONS = """# __TITLE__

This is an Autoeval project.

## Project Model

This directory is the complete Autoeval workspace. It owns its input stage,
script stage, report stage, run history, storage index, and git history.

## Versioned Stage Directories

- `input/` defines the current input version.
- `script/` defines the current script version.
- `report/` defines the current report version.

Autoeval computes a content hash for each stage directory. A run is the
combination of the input hash, script hash, report hash, environment hashes,
and runtime context hash.

Use `autoeval storage` to explicitly store the current input, script, report,
and config state as versioned assets. Running a workflow does not store stage
versions by itself.

## UI-Editable Inputs

User-editable input variables live in `input/params.json`.

The browser/API surface should render controls from `input/params.schema.json`,
write updated values to `params.json`, and trigger a run with the same script,
report, sandbox, and runtime config.

Editing `params.json` changes the input hash and creates a new input version
without changing the script or report versions.

## Editing Rules

- Put input collection and normalization changes in `input/`.
- Put core computation and evaluation changes in `script/`.
- Put presentation and report rendering changes in `report/`.
- Put sandbox, runtime, title, and metadata changes in `autoeval.json`.

Change only the stage that matches the intended version change.

## Stage Manifests

Each stage must include `stage.json` with these keys:

- `name`
- `command`
- `working_dir`
- `interface_version`

The stage command receives `${CTX}`, which points to a JSON context file.

## Runtime Context

Read paths from `${CTX}` instead of hardcoding runtime paths. Important keys:

- `input_dir`
- `input_params_path`
- `input_state_dir`
- `output_dir`
- `report_output_dir`
- `final_report_path`
- `logs_dir`

Generated state, outputs, logs, and reports must be written only to the paths
provided by `${CTX}`. Do not write generated artifacts into stage directories.

## UI Model

Treat input versions, script versions, report versions, and runs as separate
objects. Runs connect stage versions together.
"""


def die(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def now():
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def hash_json(data):
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())


def hash_dir(path):
    h = hashlib.sha256()

    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue

        rel = item.relative_to(path).as_posix()

        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue

        h.update(rel.encode())
        h.update(b"\0")
        h.update(item.read_bytes())
        h.update(b"\0")

    return h.hexdigest()


def is_project_root(path):
    path = Path(path)
    return (
        (path / PROJECT_CONFIG).is_file()
        and (path / "input").is_dir()
        and (path / "script").is_dir()
        and (path / "report").is_dir()
    )


def find_project_root(start=None):
    current = Path.cwd() if start is None else Path(start).resolve()

    for path in (current, *current.parents):
        if is_project_root(path):
            return path

    return None


def project_root(required=True):
    root = find_project_root()

    if root is None and required:
        die("not an autoeval project; run `autoeval start <project_name>` first")

    return root


def autoeval_git(args, root=None, capture=False, check=True):
    root = project_root() if root is None else Path(root)
    cmd = ["git", "-C", str(root), *args]

    try:
        if capture:
            proc = subprocess.run(
                cmd,
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return proc.stdout.strip()
        subprocess.run(cmd, check=check)
    except FileNotFoundError:
        die("required command not found: git")


def ensure_project():
    return project_root(required=True)


def ensure_workspace_contract(root=None):
    root = ensure_project() if root is None else Path(root)
    path = root / PROJECT_INSTRUCTIONS

    if not path.exists():
        cfg = read_json(root / PROJECT_CONFIG)
        path.write_text(WORKSPACE_INSTRUCTIONS.replace("__TITLE__", cfg.get("title", root.name)))


def ensure_autoeval_git_repo(root=None):
    root = ensure_project() if root is None else Path(root)

    if not (root / ".git").exists():
        die(f"{root} is missing its git repository")

    top_level = autoeval_git(["rev-parse", "--show-toplevel"], root=root, capture=True)

    if Path(top_level).resolve() != root.resolve():
        die("refusing to run git outside the autoeval project")


def config(root=None):
    root = ensure_project() if root is None else Path(root)
    return read_json(root / PROJECT_CONFIG)


def stage_manifest(stage, root=None):
    root = ensure_project() if root is None else Path(root)
    path = root / stage / "stage.json"

    if not path.exists():
        die(f"missing {path}")

    manifest = read_json(path)

    for key in ("name", "command", "working_dir", "interface_version"):
        if key not in manifest:
            die(f"{path} missing `{key}`")

    return manifest


def stage_image(stage, cfg, root=None):
    return stage_manifest(stage, root=root).get("image", cfg["sandbox"]["image"])


def runtime_context_hash(cfg):
    return hash_json(cfg.get("runtime", {}))


def compute_hashes(root=None):
    root = ensure_project() if root is None else Path(root)
    cfg = config(root)

    data = {
        "input_hash": hash_dir(root / "input"),
        "script_hash": hash_dir(root / "script"),
        "report_hash": hash_dir(root / "report"),
        "input_env_hash": hash_json({"image": stage_image("input", cfg, root=root)}),
        "script_env_hash": hash_json({"image": stage_image("script", cfg, root=root)}),
        "report_env_hash": hash_json({"image": stage_image("report", cfg, root=root)}),
        "runtime_context_hash": runtime_context_hash(cfg),
    }

    data["capsule_hash"] = hash_json(data)
    return data


def db(root=None):
    root = ensure_project() if root is None else Path(root)
    conn = sqlite3.connect(root / "index.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(root=None):
    root = ensure_project() if root is None else Path(root)
    conn = db(root)

    conn.executescript(
        """
        create table if not exists runs(
            run_id text primary key,
            capsule_hash text not null,

            input_hash text not null,
            script_hash text not null,
            report_hash text not null,

            input_env_hash text not null,
            script_env_hash text not null,
            report_env_hash text not null,
            runtime_context_hash text not null,

            git_commit text not null,
            status text not null,
            stage_status text not null,
            created_at text not null
        );

        create table if not exists input_versions(
            input_hash text primary key,
            created_at text not null,
            git_commit text not null,
            label text,
            metadata_json text not null
        );

        create table if not exists script_versions(
            script_hash text primary key,
            created_at text not null,
            git_commit text not null,
            label text,
            metadata_json text not null
        );

        create table if not exists report_versions(
            report_hash text primary key,
            created_at text not null,
            git_commit text not null,
            label text,
            metadata_json text not null
        );

        create index if not exists idx_runs_capsule
        on runs(capsule_hash, status);

        create index if not exists idx_runs_input_cache
        on runs(input_hash, input_env_hash, runtime_context_hash, status);

        create index if not exists idx_runs_script_cache
        on runs(input_hash, script_hash, input_env_hash, script_env_hash, runtime_context_hash, status);

        create index if not exists idx_runs_input_version
        on runs(input_hash, created_at);

        create index if not exists idx_runs_script_version
        on runs(script_hash, created_at);

        create index if not exists idx_runs_report_version
        on runs(report_hash, created_at);

        create index if not exists idx_input_versions_created
        on input_versions(created_at);

        create index if not exists idx_script_versions_created
        on script_versions(created_at);

        create index if not exists idx_report_versions_created
        on report_versions(created_at);
        """
    )

    conn.commit()
    conn.close()


def insert_run(meta, root=None):
    conn = db(root)

    conn.execute(
        """
        insert into runs(
            run_id,
            capsule_hash,

            input_hash,
            script_hash,
            report_hash,

            input_env_hash,
            script_env_hash,
            report_env_hash,
            runtime_context_hash,

            git_commit,
            status,
            stage_status,
            created_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meta["run_id"],
            meta["capsule_hash"],

            meta["input_hash"],
            meta["script_hash"],
            meta["report_hash"],

            meta["input_env_hash"],
            meta["script_env_hash"],
            meta["report_env_hash"],
            meta["runtime_context_hash"],

            meta["git_commit"],
            meta["status"],
            json.dumps(meta["stage_status"]),
            meta["created_at"],
        ),
    )

    conn.commit()
    conn.close()


def stage_version_metadata(stage, root=None):
    return {
        "stage": stage,
        "directory": stage,
        "manifest": stage_manifest(stage, root=root),
    }


def upsert_stage_versions(hashes, git_commit, created_at, label=None, root=None):
    conn = db(root)
    inserted = {}

    for stage in STAGES:
        table = STAGE_VERSION_TABLES[stage]
        hash_key = STAGE_HASH_KEYS[stage]
        metadata = stage_version_metadata(stage, root=root)

        cursor = conn.execute(
            f"""
            insert into {table}(
                {hash_key},
                created_at,
                git_commit,
                label,
                metadata_json
            )
            values (?, ?, ?, ?, ?)
            on conflict({hash_key}) do nothing
            """,
            (
                hashes[hash_key],
                created_at,
                git_commit,
                label,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        inserted[stage] = cursor.rowcount == 1

    conn.commit()
    conn.close()
    return inserted


def find_exact_run(hashes, root=None):
    conn = db(root)

    row = conn.execute(
        """
        select *
        from runs
        where capsule_hash = ?
          and status = 'success'
        order by created_at desc
        limit 1
        """,
        (hashes["capsule_hash"],),
    ).fetchone()

    conn.close()
    return row


def find_input_cache(hashes, root=None):
    conn = db(root)

    row = conn.execute(
        """
        select *
        from runs
        where input_hash = ?
          and input_env_hash = ?
          and runtime_context_hash = ?
          and status = 'success'
        order by created_at desc
        limit 1
        """,
        (
            hashes["input_hash"],
            hashes["input_env_hash"],
            hashes["runtime_context_hash"],
        ),
    ).fetchone()

    conn.close()
    return row


def find_script_cache(hashes, root=None):
    conn = db(root)

    row = conn.execute(
        """
        select *
        from runs
        where input_hash = ?
          and script_hash = ?
          and input_env_hash = ?
          and script_env_hash = ?
          and runtime_context_hash = ?
          and status = 'success'
        order by created_at desc
        limit 1
        """,
        (
            hashes["input_hash"],
            hashes["script_hash"],
            hashes["input_env_hash"],
            hashes["script_env_hash"],
            hashes["runtime_context_hash"],
        ),
    ).fetchone()

    conn.close()
    return row


def get_run(run_id, root=None):
    conn = db(root)
    row = conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
    conn.close()

    if not row:
        die(f"unknown run_id: {run_id}")

    return row


def copy_tree(src, dst):
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def make_ctx():
    return {
        "run_dir": "/workspace/run",

        "input_dir": "/workspace/input",
        "script_dir": "/workspace/script",
        "report_dir": "/workspace/report",
        "input_params_path": "/workspace/input/params.json",

        "input_state_dir": "/workspace/run/input_state",
        "output_dir": "/workspace/run/outputs",
        "report_output_dir": "/workspace/run/report",
        "logs_dir": "/workspace/run/logs",

        "final_report_path": "/workspace/run/final_report.md",
    }


def container_workdir(stage, working_dir):
    p = working_dir.strip().replace("\\", "/")

    if p == stage:
        return f"/workspace/{stage}"

    if p.startswith(f"{stage}/"):
        suffix = p.removeprefix(f"{stage}/")
        return f"/workspace/{stage}/{suffix}"

    if p.startswith("/workspace/"):
        return p

    return f"/workspace/{stage}"


def run_stage(stage, run_dir, root=None):
    root = ensure_project() if root is None else Path(root)
    cfg = config(root)
    manifest = stage_manifest(stage, root=root)

    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    image = stage_image(stage, cfg, root=root)
    command = manifest["command"].replace("${CTX}", "/workspace/run/ctx.json")
    workdir = container_workdir(stage, manifest["working_dir"])

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        cfg["sandbox"].get("network", "none"),
        "--cpus",
        str(cfg["sandbox"].get("cpus", "1")),
        "--memory",
        cfg["sandbox"].get("memory", "512m"),

        "-v",
        f"{(root / 'input').resolve()}:/workspace/input:ro",
        "-v",
        f"{(root / 'script').resolve()}:/workspace/script:ro",
        "-v",
        f"{(root / 'report').resolve()}:/workspace/report:ro",
        "-v",
        f"{run_dir.resolve()}:/workspace/run:rw",

        "-w",
        workdir,

        image,
        "sh",
        "-lc",
        command,
    ]

    with (logs_dir / f"{stage}.stdout.log").open("w") as stdout:
        with (logs_dir / f"{stage}.stderr.log").open("w") as stderr:
            proc = subprocess.run(docker_cmd, stdout=stdout, stderr=stderr)

    return proc.returncode


def ensure_docker_ready():
    if shutil.which("docker") is None:
        die("required command not found: docker")

    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "docker daemon is not reachable"
        die(f"docker is not usable: {detail}")


def current_autoeval_commit(root=None):
    root = ensure_project() if root is None else Path(root)
    ensure_autoeval_git_repo(root)
    return autoeval_git(["rev-parse", "HEAD"], root=root, capture=True)


def git_status(paths, root=None):
    root = ensure_project() if root is None else Path(root)
    ensure_autoeval_git_repo(root)
    return autoeval_git(["status", "--porcelain", "--", *paths], root=root, capture=True)


def warn_unstored_workspace_changes(root=None):
    root = ensure_project() if root is None else Path(root)

    if not git_status(STORAGE_PATHS, root=root):
        return

    print(
        "note: executing unstored stage/config changes; run `autoeval storage` "
        "to store current input/script/report versions.",
        file=sys.stderr,
    )


def git_commit_storage(message, root=None):
    root = ensure_project() if root is None else Path(root)
    ensure_autoeval_git_repo(root)

    autoeval_git(["add", *STORAGE_PATHS], root=root)

    if not autoeval_git(["diff", "--cached", "--name-only", "--", *STORAGE_PATHS], root=root, capture=True):
        return current_autoeval_commit(root), False

    autoeval_git(["commit", "-m", message, "--", *STORAGE_PATHS], root=root)
    return current_autoeval_commit(root), True


def git_commit_run(run_id, root=None):
    root = ensure_project() if root is None else Path(root)
    ensure_autoeval_git_repo(root)

    run_path = f"runs/{run_id}"
    autoeval_git(["add", run_path], root=root)
    autoeval_git(["commit", "-m", f"autoeval run {run_id}", "--only", "--", run_path], root=root)

    commit = current_autoeval_commit(root)
    autoeval_git(["tag", f"run-{run_id}", commit], root=root)

    return commit


def ensure_git_identity(root):
    name = autoeval_git(["config", "user.name"], root=root, capture=True, check=False)
    email = autoeval_git(["config", "user.email"], root=root, capture=True, check=False)

    if not name:
        autoeval_git(["config", "user.name", "Autoeval"], root=root)

    if not email:
        autoeval_git(["config", "user.email", "autoeval@local"], root=root)


def write_default_project(root, title):
    for stage in STAGES:
        (root / stage).mkdir(parents=True)

    (root / "runs").mkdir()

    write_json(
        root / PROJECT_CONFIG,
        {
            "title": title,
            "description": "",
            "sandbox": {
                "image": "python:3.12-slim",
                "network": "none",
                "cpus": "1",
                "memory": "512m",
            },
            "runtime": {},
        },
    )

    (root / PROJECT_INSTRUCTIONS).write_text(WORKSPACE_INSTRUCTIONS.replace("__TITLE__", title))

    write_json(
        root / "input" / "stage.json",
        {
            "name": "input",
            "command": "python input.py --ctx ${CTX}",
            "working_dir": "input",
            "interface_version": "1",
        },
    )
    write_json(root / "input" / "params.json", DEFAULT_INPUT_PARAMS)
    write_json(root / "input" / "params.schema.json", DEFAULT_INPUT_PARAMS_SCHEMA)

    write_json(
        root / "script" / "stage.json",
        {
            "name": "script",
            "command": "python script.py --ctx ${CTX}",
            "working_dir": "script",
            "interface_version": "1",
        },
    )

    write_json(
        root / "report" / "stage.json",
        {
            "name": "report",
            "command": "python report.py --ctx ${CTX}",
            "working_dir": "report",
            "interface_version": "1",
        },
    )

    (root / "input" / "input.py").write_text(
        """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
args = parser.parse_args()

ctx = json.loads(Path(args.ctx).read_text())
params = json.loads(Path(ctx["input_params_path"]).read_text())

state_dir = Path(ctx["input_state_dir"])
state_dir.mkdir(parents=True, exist_ok=True)

(state_dir / "input.json").write_text(
    json.dumps(params, indent=2) + "\\n"
)
"""
    )

    (root / "script" / "script.py").write_text(
        """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
args = parser.parse_args()

ctx = json.loads(Path(args.ctx).read_text())

input_data = json.loads(
    (Path(ctx["input_state_dir"]) / "input.json").read_text()
)

output_dir = Path(ctx["output_dir"])
output_dir.mkdir(parents=True, exist_ok=True)

(output_dir / "result.json").write_text(
    json.dumps(
        {
            "input": input_data,
            "result": "hello from script"
        },
        indent=2,
    ) + "\\n"
)
"""
    )

    (root / "report" / "report.py").write_text(
        """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
args = parser.parse_args()

ctx = json.loads(Path(args.ctx).read_text())

result = json.loads(
    (Path(ctx["output_dir"]) / "result.json").read_text()
)

Path(ctx["report_output_dir"]).mkdir(parents=True, exist_ok=True)

Path(ctx["final_report_path"]).write_text(
    "# Autoeval Report\\n\\n"
    f"Input message: {result['input']['message']}\\n\\n"
    f"Result: {result['result']}\\n"
)
"""
    )

    (root / ".gitignore").write_text(
        """index.sqlite
server/
__pycache__/
*.pyc
"""
    )


def create_project(root, title):
    root = Path(root)

    if root.exists() and any(root.iterdir()):
        die(f"{root} already exists and is not empty")

    root.mkdir(parents=True, exist_ok=True)
    write_default_project(root, title)

    autoeval_git(["init", "-b", "main"], root=root)
    ensure_autoeval_git_repo(root)
    ensure_git_identity(root)
    init_db(root)

    autoeval_git(["add", *STORAGE_PATHS], root=root)
    autoeval_git(["commit", "-m", "autoeval start"], root=root)

    return root


def start_cmd(args):
    target = Path(args.project_name).expanduser()
    title = args.title or target.name
    root = create_project(target, title)
    print(f"started autoeval project: {root}")


def init_cmd(args):
    root = Path.cwd()

    if is_project_root(root):
        die("current directory is already an autoeval project")

    create_project(root, args.title or root.name)
    print(f"initialized autoeval project: {root}")


def run_cmd(args):
    root = ensure_project()
    ensure_workspace_contract(root)
    init_db(root)

    hashes = compute_hashes(root)

    existing = find_exact_run(hashes, root)

    if existing and not args.force:
        print(f"duplicate capsule: {existing['run_id']}")
        print(f"status: {existing['status']}")
        print(f"git_commit: {existing['git_commit']}")
        return

    warn_unstored_workspace_changes(root)
    ensure_docker_ready()

    run_id = f"{now()}_{hashes['capsule_hash'][:8]}_{uuid.uuid4().hex[:6]}"
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)

    for name in ("input_state", "outputs", "report", "logs"):
        (run_dir / name).mkdir()

    write_json(run_dir / "ctx.json", make_ctx())

    status = "success"
    stage_status = {}

    script_cache = find_script_cache(hashes, root)

    if script_cache:
        cached_run = root / "runs" / script_cache["run_id"]

        copy_tree(cached_run / "input_state", run_dir / "input_state")
        copy_tree(cached_run / "outputs", run_dir / "outputs")

        stage_status["input"] = f"reused:{script_cache['run_id']}"
        stage_status["script"] = f"reused:{script_cache['run_id']}"

    else:
        input_cache = find_input_cache(hashes, root)

        if input_cache:
            cached_run = root / "runs" / input_cache["run_id"]
            copy_tree(cached_run / "input_state", run_dir / "input_state")
            stage_status["input"] = f"reused:{input_cache['run_id']}"

        else:
            code = run_stage("input", run_dir, root)
            stage_status["input"] = "success" if code == 0 else f"failed:{code}"

            if code != 0:
                status = "failed"

        if status == "success":
            code = run_stage("script", run_dir, root)
            stage_status["script"] = "success" if code == 0 else f"failed:{code}"

            if code != 0:
                status = "failed"

    if status == "success":
        code = run_stage("report", run_dir, root)
        stage_status["report"] = "success" if code == 0 else f"failed:{code}"

        if code != 0:
            status = "failed"

    created_at = now()

    meta = {
        "run_id": run_id,
        **hashes,
        "git_commit": "",
        "status": status,
        "stage_status": stage_status,
        "created_at": created_at,
    }

    write_json(run_dir / "run.json", meta)

    commit = git_commit_run(run_id, root)
    meta["git_commit"] = commit

    insert_run(meta, root)

    print(f"run_id: {run_id}")
    print(f"status: {status}")
    print(f"capsule_hash: {hashes['capsule_hash']}")
    print(f"git_commit: {commit}")
    print(f"run_dir: {run_dir.relative_to(root)}")

    report_path = run_dir / "final_report.md"

    if report_path.exists():
        print(f"report: {report_path.relative_to(root)}")


def status_cmd(args):
    root = ensure_project()
    init_db(root)

    conn = db(root)

    rows = conn.execute(
        """
        select run_id, status, capsule_hash, git_commit, stage_status, created_at
        from runs
        order by created_at desc
        limit ?
        """,
        (args.limit,),
    ).fetchall()

    conn.close()

    if not rows:
        print("no runs")
        return

    for row in rows:
        print(
            f"{row['created_at']}  "
            f"{row['status']:<7}  "
            f"{row['run_id']}  "
            f"{row['capsule_hash'][:12]}  "
            f"{row['git_commit'][:12]}"
        )


def hash_cmd(args):
    root = ensure_project()
    hashes = compute_hashes(root)

    for key in sorted(hashes):
        print(f"{key}: {hashes[key]}")


def storage_cmd(args):
    root = ensure_project()
    ensure_workspace_contract(root)
    ensure_autoeval_git_repo(root)
    init_db(root)

    hashes = compute_hashes(root)
    created_at = now()
    message = args.message or "autoeval storage"
    commit, committed = git_commit_storage(message, root)
    inserted = upsert_stage_versions(hashes, commit, created_at, label=args.label, root=root)

    print(f"storage_commit: {commit}")
    print(f"committed: {'yes' if committed else 'no'}")
    print(f"input_hash: {hashes['input_hash']}")
    print(f"script_hash: {hashes['script_hash']}")
    print(f"report_hash: {hashes['report_hash']}")
    print(f"capsule_hash: {hashes['capsule_hash']}")

    for stage in STAGES:
        status = "stored" if inserted[stage] else "existing"
        print(f"{stage}_version: {status}")


def diff_cmd(args):
    root = ensure_project()
    ensure_autoeval_git_repo(root)
    init_db(root)

    a = get_run(args.run_a, root)
    b = get_run(args.run_b, root)

    autoeval_git(
        [
            "diff",
            a["git_commit"],
            b["git_commit"],
            "--",
            *STORAGE_PATHS,
        ],
        root=root,
        check=False,
    )


def restore_cmd(args):
    root = ensure_project()
    ensure_autoeval_git_repo(root)
    init_db(root)

    run = get_run(args.run_id, root)

    autoeval_git(
        [
            "checkout",
            run["git_commit"],
            "--",
            *STORAGE_PATHS,
        ],
        root=root,
    )

    print(f"restored input/script/report/config from {args.run_id}")


def serve_cmd(args):
    from .server import serve

    serve(args.host, args.port, args.allow_origin)


def build_parser():
    parser = argparse.ArgumentParser(prog="autoeval")
    sub = parser.add_subparsers(required=True)

    start = sub.add_parser("start")
    start.add_argument("project_name")
    start.add_argument("--title")
    start.set_defaults(fn=start_cmd)

    init = sub.add_parser("init")
    init.add_argument("--title")
    init.set_defaults(fn=init_cmd)

    run = sub.add_parser("run")
    run.add_argument("--force", action="store_true")
    run.set_defaults(fn=run_cmd)

    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(fn=status_cmd)

    hashes = sub.add_parser("hash")
    hashes.set_defaults(fn=hash_cmd)

    storage = sub.add_parser("storage")
    storage.add_argument("--label")
    storage.add_argument("--message")
    storage.set_defaults(fn=storage_cmd)

    diff = sub.add_parser("diff")
    diff.add_argument("run_a")
    diff.add_argument("run_b")
    diff.set_defaults(fn=diff_cmd)

    restore = sub.add_parser("restore")
    restore.add_argument("run_id")
    restore.set_defaults(fn=restore_cmd)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="allowed browser origin for cross-origin API access",
    )
    serve.set_defaults(fn=serve_cmd)

    return parser


def main():
    args = build_parser().parse_args()
    args.fn(args)
