import hashlib
import json
import os
import platform
import signal
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .store import db
from .workspace import (
    APP_ENV,
    EXPERIMENT_DIR,
    PARAMS_FILE,
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
RUN_CONTEXT = {
    "run_dir": "/workspace/run",
    "script_dir": "/workspace/source/experiment",
    "app_env_path": "/workspace/.env",
    "script_params_path": "/workspace/source/.autoexp/params.json",
    "output_dir": "/workspace/run/output",
    "logs_dir": "/workspace/run/logs",
}


# ======================================================================
#  Hashing: identity of inputs (capsule) and outputs
# ======================================================================

def hash_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hash_dir(path):
    """Hash a directory's tracked files (name + bytes), skipping .gitignored ones."""
    digest = hashlib.sha256()
    root = path.parent
    if (root / ".gitignore").is_symlink():
        raise ValueError(".gitignore must not be a symlink")
    patterns = gitignore_patterns(root)
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"source contains unsupported symlink: {item.relative_to(path)}")
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
    if not isinstance(cfg, dict):
        raise ValueError(f"{PROJECT_CONFIG} must contain a JSON object")
    manifest = script_manifest(root)
    sandbox = cfg.get("sandbox") if isinstance(cfg.get("sandbox"), dict) else {}
    data = {
        "script_hash": hash_dir(root / EXPERIMENT_DIR),
        "script_env_hash": hash_json({
            "runner": cfg.get("runner", "local"),
            "image": manifest.get("image") or sandbox.get("image"),
        }),
        "runtime_context_hash": hash_json({
            "runtime": cfg.get("runtime", {}),
            "params": read_json(root / PARAMS_FILE),
            "stage": manifest,
        }),
    }
    data["capsule_hash"] = hash_json(data)
    return data


def hash_path(digest, path, base):
    if path.is_symlink():
        raise ValueError(f"output contains unsupported symlink: {path.relative_to(base)}")
    digest.update(path.relative_to(base).as_posix().encode())
    digest.update(b"\0")
    if path.is_dir():
        digest.update(b"dir\0")
    else:
        digest.update(b"file\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")


def hash_run_output(run_dir):
    """Hash a run's output artifacts, so identical results can be recognized."""
    digest = hashlib.sha256()
    path = Path(run_dir) / "output"
    digest.update(b"output\0")
    if path.is_symlink():
        raise ValueError("run output directory must not be a symlink")
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
        """select * from runs where capsule_hash = ? and status = 'success'
           order by created_at desc, rowid desc""",
        (hashes["capsule_hash"],),
    ).fetchall()
    conn.close()
    for row in rows:
        run = dict(row)
        run_dir = run_dir_for(run, root)
        if run_dir.exists():
            try:
                prior_hash = run.get("output_hash") or hash_run_output(run_dir)
            except ValueError:
                continue
            if prior_hash == output_hash:
                return run
    return None


# ======================================================================
#  Docker runner
# ======================================================================

def _wait(proc, timeout_sec=None):
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()
        raise


def run_script(run_dir, root=None, source_root=None, *, extra_env=None, timeout_sec=None):
    """Execute the experiment inside a docker sandbox; return the exit code."""
    root = resolve_root(root)
    source_root = root if source_root is None else Path(source_root)
    cfg = read_json(source_root / PROJECT_CONFIG)
    manifest = script_manifest(source_root)

    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if not workdir.startswith("/workspace/"):
        workdir = f"/workspace/source/{workdir if workdir.startswith(EXPERIMENT_DIR) else EXPERIMENT_DIR}"

    sandbox = cfg["sandbox"]
    container_name = f"autoexp-{Path(run_dir).name}"
    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", sandbox.get("network", "none"),
        "--cpus", str(sandbox.get("cpus", "1")),
        "--memory", sandbox.get("memory", "512m"),
    ]
    app_env_path = root / APP_ENV
    if app_env_path.exists():
        cmd += ["--env-file", str(app_env_path.resolve()),
                "-v", f"{app_env_path.resolve()}:/workspace/.env:ro"]
    for key, value in (extra_env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [
        "-e", "AUTOEXP_OUTPUT_DIR=/workspace/run/output",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{source_root.resolve()}:/workspace/source:ro",
        "-v", f"{Path(run_dir).resolve()}:/workspace/run:rw",
        "-w", workdir,
        manifest.get("image", sandbox["image"]),
        "sh", "-lc",
        manifest["command"].replace("${CTX}", "/workspace/run/ctx.json"),
    ]
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout, (logs / "script.stderr.log").open("w") as stderr:
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            return _wait(proc, timeout_sec)
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            raise


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


# ======================================================================
#  Local runner
# ======================================================================

def app_env(root):
    """Parse .env into a dict of {KEY: value} (quotes stripped)."""
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
        "script_dir": str(source_root / EXPERIMENT_DIR),
        "app_env_path": str((Path(root) / APP_ENV).resolve()),
        "script_params_path": str(source_root / PARAMS_FILE),
        "output_dir": str(run_dir / "output"),
        "logs_dir": str(run_dir / "logs"),
    }


def local_workdir(manifest, run_dir, source_root):
    """Translate the manifest working_dir (which may use container paths) to a host path."""
    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if workdir.startswith("/workspace/source/"):
        return Path(source_root) / workdir.removeprefix("/workspace/source/")
    if workdir.startswith("/workspace/run"):
        return Path(run_dir) / workdir.removeprefix("/workspace/run").lstrip("/")
    if workdir.startswith(EXPERIMENT_DIR):
        return Path(source_root) / workdir
    return Path(source_root) / EXPERIMENT_DIR


def run_script_local(run_dir, root=None, source_root=None, *, extra_env=None, timeout_sec=None):
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

    env = os.environ | app_env(root) | (extra_env or {}) | {
        "AUTOEXP_RUN_DIR": str(Path(run_dir).resolve()),
        "AUTOEXP_SCRIPT_DIR": str((source_root / EXPERIMENT_DIR).resolve()),
        "AUTOEXP_OUTPUT_DIR": str((Path(run_dir) / "output").resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    logs = Path(run_dir) / "logs"
    with (logs / "script.stdout.log").open("w") as stdout, (logs / "script.stderr.log").open("w") as stderr:
        proc = subprocess.Popen(
            command,
            cwd=local_workdir(manifest, run_dir, source_root),
            env=env, shell=True, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        return _wait(proc, timeout_sec)


def runner_type(root, source_root=None):
    """Resolve the effective runner, failing if docker is requested but unavailable."""
    source_root = Path(root) if source_root is None else Path(source_root)
    requested = read_json(source_root / PROJECT_CONFIG).get("runner", "local")
    if requested not in {"docker", "local"}:
        die(f"{PROJECT_CONFIG} runner must be one of: docker, local")
    if requested == "local":
        return "local"
    ok, message = docker_ready()
    if not ok:
        die(message)
    return "docker"


def runner_identity(root, runner=None, source_root=None):
    """Return the concrete local runtime or configured container identity."""
    source_root = Path(root) if source_root is None else Path(source_root)
    runner = runner or runner_type(root, source_root)
    if runner == "docker":
        cfg = read_json(source_root / PROJECT_CONFIG)
        manifest = script_manifest(source_root)
        return f"docker:{manifest.get('image', cfg['sandbox']['image'])}"
    return f"{sys.implementation.name}:{platform.python_version()}:{Path(sys.executable).resolve()}"
