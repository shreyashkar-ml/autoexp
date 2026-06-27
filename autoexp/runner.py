import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .store import db
from .workspace import (
    APP_ENV,
    PROJECT_CONFIG,
    die,
    gitignore_patterns,
    ignored,
    read_json,
    resolve_root,
    run_dir_for,
    script_manifest,
)


# Paths *inside* the sandbox container. The host mounts the run/script dirs here.
RUN_ARTIFACT_PATHS = ("output",)
RUN_CONTEXT = {
    "run_dir": "/workspace/run",
    "script_dir": "/workspace/script",
    "app_env_path": "/workspace/app.env",
    "script_params_path": "/workspace/script/params.json",
    "output_dir": "/workspace/run/output",
    "logs_dir": "/workspace/run/logs",
}


# --- hashing: identity of inputs (capsule) and outputs ----------------------

def hash_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hash_dir(path):
    """Hash a directory's tracked files (name + bytes), skipping .gitignored ones."""
    digest = hashlib.sha256()
    root = path.parent
    patterns = gitignore_patterns(root)
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(path).as_posix()
        root_rel = item.relative_to(root).as_posix()
        if ignored(root_rel, patterns):
            continue
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def compute_hashes(root=None):
    """The capsule: a hash of script + environment + runtime that identifies a run's inputs."""
    root = resolve_root(root)
    cfg = read_json(root / PROJECT_CONFIG)
    manifest = script_manifest(root)
    data = {
        "script_hash": hash_dir(root / "script"),
        "script_env_hash": hash_json({
            "runner": cfg.get("runner", "local"),
            "image": manifest.get("image", cfg["sandbox"]["image"]),
        }),
        "runtime_context_hash": hash_json(cfg.get("runtime", {})),
    }
    data["capsule_hash"] = hash_json(data)
    return data


def hash_path(digest, path, base):
    digest.update(path.relative_to(base).as_posix().encode())
    digest.update(b"\0")
    if path.is_dir():
        digest.update(b"dir\0")
    else:
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")


def hash_run_output(run_dir):
    """Hash a run's output artifacts, so identical results can be recognized."""
    digest = hashlib.sha256()
    run_dir = Path(run_dir)
    for label in RUN_ARTIFACT_PATHS:
        path = run_dir / label
        digest.update(label.encode())
        digest.update(b"\0")
        if not path.exists():
            digest.update(b"missing\0")
        elif path.is_file():
            hash_path(digest, path, path.parent)
        else:
            digest.update(b"dir\0")
            for item in sorted(path.rglob("*")):
                hash_path(digest, item, path)
    return digest.hexdigest()


def find_duplicate_output_run(hashes, output_hash, root=None):
    """A prior successful run with the same inputs *and* the same output, if any."""
    root = resolve_root(root)
    conn = db(root)
    rows = conn.execute(
        "select * from runs where capsule_hash = ? and status = 'success' order by created_at desc",
        (hashes["capsule_hash"],),
    ).fetchall()
    conn.close()
    for row in rows:
        run = dict(row)
        run_dir = run_dir_for(run, root)
        if run_dir.exists() and (run.get("output_hash") or hash_run_output(run_dir)) == output_hash:
            return run
    return None


# --- docker runner ----------------------------------------------------------

def run_script(run_dir, root=None, source_root=None):
    """Execute the experiment inside a docker sandbox; return the exit code."""
    root = resolve_root(root)
    source_root = root if source_root is None else Path(source_root)
    cfg = read_json(source_root / PROJECT_CONFIG)
    manifest = script_manifest(source_root)

    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if not workdir.startswith("/workspace/"):
        workdir = f"/workspace/{workdir if workdir.startswith('script') else 'script'}"

    sandbox = cfg["sandbox"]
    cmd = [
        "docker", "run", "--rm",
        "--network", sandbox.get("network", "none"),
        "--cpus", str(sandbox.get("cpus", "1")),
        "--memory", sandbox.get("memory", "512m"),
    ]
    app_env_path = root / APP_ENV
    if app_env_path.exists():
        cmd += ["--env-file", str(app_env_path.resolve()),
                "-v", f"{app_env_path.resolve()}:/workspace/app.env:ro"]
    cmd += [
        "-e", "AUTOEXP_OUTPUT_DIR=/workspace/run/output",
        "-v", f"{(source_root / 'script').resolve()}:/workspace/script:ro",
        "-v", f"{Path(run_dir).resolve()}:/workspace/run:rw",
        "-w", workdir,
        manifest.get("image", sandbox["image"]),
        "sh", "-lc",
        manifest["command"].replace("${CTX}", "/workspace/run/ctx.json"),
    ]
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout, (logs / "script.stderr.log").open("w") as stderr:
        return subprocess.run(cmd, stdout=stdout, stderr=stderr).returncode


def docker_ready():
    """(ok, message): whether docker is installed and its daemon is reachable."""
    if shutil.which("docker") is None:
        return False, "required command not found: docker"
    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except subprocess.CalledProcessError as exc:
        return False, f"docker is not usable: {exc.stderr.strip() or 'docker daemon is not reachable'}"
    return True, ""


# --- local runner -----------------------------------------------------------

def app_env(root):
    """Parse app.env into a dict of {KEY: value} (quotes stripped)."""
    path = Path(root) / APP_ENV
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def local_run_context(run_dir, source_root, root):
    """The host-path equivalent of RUN_CONTEXT for the local (non-docker) runner."""
    run_dir = Path(run_dir).resolve()
    source_root = Path(source_root).resolve()
    return {
        "run_dir": str(run_dir),
        "script_dir": str(source_root / "script"),
        "app_env_path": str((Path(root) / APP_ENV).resolve()),
        "script_params_path": str(source_root / "script" / "params.json"),
        "output_dir": str(run_dir / "output"),
        "logs_dir": str(run_dir / "logs"),
    }


def local_workdir(manifest, run_dir, source_root):
    """Translate the manifest working_dir (which may use container paths) to a host path."""
    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if workdir.startswith("/workspace/script"):
        return Path(source_root) / "script" / workdir.removeprefix("/workspace/script").lstrip("/")
    if workdir.startswith("/workspace/run"):
        return Path(run_dir) / workdir.removeprefix("/workspace/run").lstrip("/")
    if workdir.startswith("script"):
        return Path(source_root) / workdir
    return Path(source_root) / "script"


def run_script_local(run_dir, root=None, source_root=None):
    """Execute the experiment directly on the host; return the exit code."""
    root = resolve_root(root)
    source_root = root if source_root is None else Path(source_root)
    manifest = script_manifest(source_root)

    ctx_path = (Path(run_dir) / "ctx.json").resolve()
    command = manifest["command"].replace("${CTX}", shlex.quote(str(ctx_path)))
    for python_alias in ("python ", "python3 "):
        if command.startswith(python_alias):
            command = f"{shlex.quote(sys.executable)} {command.removeprefix(python_alias)}"
            break

    env = os.environ.copy()
    env.update(app_env(root))
    env.update({
        "AUTOEXP_RUN_DIR": str(Path(run_dir).resolve()),
        "AUTOEXP_SCRIPT_DIR": str((source_root / "script").resolve()),
        "AUTOEXP_OUTPUT_DIR": str((Path(run_dir) / "output").resolve()),
    })
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout, (logs / "script.stderr.log").open("w") as stderr:
        return subprocess.run(
            command,
            cwd=local_workdir(manifest, run_dir, source_root),
            env=env, shell=True, stdout=stdout, stderr=stderr,
        ).returncode


def runner_type(root):
    """Resolve the effective runner, failing if docker is requested but unavailable."""
    requested = read_json(Path(root) / PROJECT_CONFIG).get("runner", "local")
    if requested not in {"docker", "local"}:
        die("autoexp.json runner must be one of: docker, local")
    if requested == "local":
        return "local"
    ok, message = docker_ready()
    if not ok:
        die(message)
    return "docker"
