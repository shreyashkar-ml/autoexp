import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .store import db
from .workspace import APP_ENV, PROJECT_CONFIG, die, gitignore_patterns, ignored, project_root, read_json, script_manifest


RUN_ARTIFACT_PATHS = ("output",)
RUN_CONTEXT = {
    "run_dir": "/workspace/run",
    "script_dir": "/workspace/script",
    "app_env_path": "/workspace/app.env",
    "script_params_path": "/workspace/script/params.json",
    "output_dir": "/workspace/run/output",
    "logs_dir": "/workspace/run/logs",
}
def hash_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hash_dir(path):
    h, root = hashlib.sha256(), path.parent
    patterns = gitignore_patterns(root)
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        rel, root_rel = item.relative_to(path).as_posix(), item.relative_to(root).as_posix()
        if ignored(root_rel, patterns):
            continue
        h.update(rel.encode())
        h.update(b"\0")
        h.update(item.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def compute_hashes(root=None):
    root = project_root() if root is None else Path(root)
    cfg, manifest = read_json(root / PROJECT_CONFIG), script_manifest(root)
    data = {
        "script_hash": hash_dir(root / "script"),
        "script_env_hash": hash_json({"runner": cfg.get("runner", "local"), "image": manifest.get("image", cfg["sandbox"]["image"])}),
        "runtime_context_hash": hash_json(cfg.get("runtime", {})),
    }
    data["capsule_hash"] = hash_json(data)
    return data


def hash_path(h, path, base):
    h.update(path.relative_to(base).as_posix().encode())
    h.update(b"\0")
    if path.is_dir():
        h.update(b"dir\0")
    else:
        h.update(b"file\0")
        h.update(path.read_bytes())
        h.update(b"\0")


def hash_run_output(run_dir):
    h, run_dir = hashlib.sha256(), Path(run_dir)
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
    cfg, manifest = read_json(source_root / PROJECT_CONFIG), script_manifest(source_root)
    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if not workdir.startswith("/workspace/"):
        workdir = f"/workspace/{workdir if workdir.startswith('script') else 'script'}"
    cmd = [
        "docker", "run", "--rm", "--network", cfg["sandbox"].get("network", "none"),
        "--cpus", str(cfg["sandbox"].get("cpus", "1")), "--memory", cfg["sandbox"].get("memory", "512m"),
    ]
    app_env_path = root / APP_ENV
    if app_env_path.exists():
        cmd += ["--env-file", str(app_env_path.resolve()), "-v", f"{app_env_path.resolve()}:/workspace/app.env:ro"]
    cmd += [
        "-e", "AUTOEXP_OUTPUT_DIR=/workspace/run/output",
        "-v", f"{(source_root / 'script').resolve()}:/workspace/script:ro",
        "-v", f"{Path(run_dir).resolve()}:/workspace/run:rw", "-w", workdir,
        manifest.get("image", cfg["sandbox"]["image"]), "sh", "-lc",
        manifest["command"].replace("${CTX}", "/workspace/run/ctx.json"),
    ]
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout, (logs / "script.stderr.log").open("w") as stderr:
        return subprocess.run(cmd, stdout=stdout, stderr=stderr).returncode


def docker_ready():
    if shutil.which("docker") is None:
        return False, "required command not found: docker"
    try:
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        return False, f"docker is not usable: {exc.stderr.strip() or 'docker daemon is not reachable'}"
    return True, ""


def app_env(root):
    path, values = Path(root) / APP_ENV, {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def local_run_context(run_dir, source_root, root):
    run_dir, source_root = Path(run_dir).resolve(), Path(source_root).resolve()
    return {
        "run_dir": str(run_dir), "script_dir": str(source_root / "script"),
        "app_env_path": str((Path(root) / APP_ENV).resolve()),
        "script_params_path": str(source_root / "script" / "params.json"),
        "output_dir": str(run_dir / "output"), "logs_dir": str(run_dir / "logs"),
    }


def local_workdir(manifest, run_dir, source_root):
    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if workdir.startswith("/workspace/script"):
        return Path(source_root) / "script" / workdir.removeprefix("/workspace/script").lstrip("/")
    if workdir.startswith("/workspace/run"):
        return Path(run_dir) / workdir.removeprefix("/workspace/run").lstrip("/")
    return Path(source_root) / workdir if workdir.startswith("script") else Path(source_root) / "script"


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
    env.update({"AUTOEXP_RUN_DIR": str(Path(run_dir).resolve()), "AUTOEXP_SCRIPT_DIR": str((source_root / "script").resolve()), "AUTOEXP_OUTPUT_DIR": str((Path(run_dir) / "output").resolve())})
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout, (logs / "script.stderr.log").open("w") as stderr:
        return subprocess.run(command, cwd=local_workdir(manifest, run_dir, source_root), env=env, shell=True, stdout=stdout, stderr=stderr).returncode


def runner_type(root):
    requested = read_json(Path(root) / PROJECT_CONFIG).get("runner", "local")
    if requested not in {"docker", "local"}:
        die("autoexp.json runner must be one of: docker, local")
    if requested == "local":
        return "local"
    ok, message = docker_ready()
    if not ok:
        die(message)
    return "docker"
