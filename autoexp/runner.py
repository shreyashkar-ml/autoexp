import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .store import db
from .workspace import (
    PARAMS_FILE, PROJECT_CONFIG, experiment_config, experiment_id, manifest_files,
    read_json, repository_root, resolve_root, run_dir_for, safe_repository_path, script_manifest,
)

RUN_CONTEXT = {
    "run_dir": "/workspace/run", "script_dir": "/workspace/source",
    "app_env_path": "", "script_params_path": "/workspace/source/.autoexp/params.json",
    "output_dir": "/workspace/run/output", "logs_dir": "/workspace/run/logs",
}
SECRET_KEY = re.compile(r"(?:secret|token|password|passwd|api[_-]?key|credential)", re.I)


def hash_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hash_dir(path):
    digest = hashlib.sha256()
    path = Path(path)
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"source contains unsupported symlink: {item.relative_to(path)}")
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode() + b"\0")
            digest.update(item.read_bytes() + b"\0")
    return digest.hexdigest()


def compute_hashes(root=None):
    root = Path(root) if root is not None else resolve_root()
    cfg = read_json(root / PROJECT_CONFIG)
    manifest = script_manifest(root)
    sandbox = cfg.get("sandbox") if isinstance(cfg.get("sandbox"), dict) else {}
    from .snapshots import snapshot_hashes
    source = snapshot_hashes(root)
    data = {
        "script_hash": source["script_hash"],
        "script_env_hash": hash_json({"runner": cfg.get("runner", "local"), "image": manifest.get("image") or sandbox.get("image")}),
        "runtime_context_hash": hash_json({"runtime": cfg.get("runtime", {}), "params": read_json(root / PARAMS_FILE), "stage": manifest}),
    }
    data["capsule_hash"] = hash_json(data)
    return data


def hash_path(digest, path, base):
    if path.is_symlink():
        raise ValueError(f"output contains unsupported symlink: {path.relative_to(base)}")
    digest.update(path.relative_to(base).as_posix().encode() + b"\0")
    if path.is_dir():
        digest.update(b"dir\0")
    else:
        digest.update(b"file\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")


def hash_run_output(run_dir):
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
    root = resolve_root(root)
    conn = db()
    rows = conn.execute(
        """select * from runs where experiment_id = ? and capsule_hash = ?
           and status = 'success' order by created_at desc, rowid desc""",
        (experiment_id(root), hashes["capsule_hash"]),
    ).fetchall()
    conn.close()
    for row in rows:
        run = dict(row)
        path = run_dir_for(run, root)
        if path.exists():
            try:
                prior_hash = run.get("output_hash") or hash_run_output(path)
            except ValueError:
                continue
            if prior_hash == output_hash:
                return run
    return None


def _redaction_values(values):
    return {str(value) for value in values if value}


def redaction_env_values(root):
    """Return values whose key or declared source marks them as secret."""
    root = resolve_root(root)
    environment = app_env(root)
    secret_names = {
        key["name"]
        for item in manifest_files(root)
        if item["role"] == "secret-source"
        for key in item["secret_keys"]
    }
    inputs = experiment_config(root).get("external_inputs", [])
    if isinstance(inputs, dict):
        inputs = [
            {"name": name, **spec} if isinstance(spec, dict) else {"name": name}
            for name, spec in inputs.items()
        ]
    secret_names.update(
        item["name"]
        for item in inputs
        if isinstance(item, dict)
        and item.get("kind") == "secret"
        and isinstance(item.get("name"), str)
    )
    secret_names.update(name for name in environment if SECRET_KEY.search(name))
    return tuple(value for name, value in environment.items() if name in secret_names)


def _redact(text, values):
    for value in sorted(_redaction_values(values), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def redact_secrets(text, root, secret_values=()):
    """Remove known secret-source values before text enters durable evidence."""
    return _redact(str(text), [*redaction_env_values(root), *secret_values])


def _capture(proc, logs, values, timeout_sec=None):
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        (logs / "script.stdout.log").write_text(_redact(stdout or "", values))
        (logs / "script.stderr.log").write_text(_redact(stderr or "", values))
        raise
    (logs / "script.stdout.log").write_text(_redact(stdout or "", values))
    (logs / "script.stderr.log").write_text(_redact(stderr or "", values))
    return proc.returncode


def scrub_secrets(run_dir, root, secret_values=()):
    values = [*redaction_env_values(root), *secret_values]
    secrets = [
        value.encode() for value in sorted(_redaction_values(values), key=len, reverse=True)
    ]
    for directory in ("output", "logs", "report"):
        base = Path(run_dir) / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            clean = data
            for value in secrets:
                clean = clean.replace(value, b"[redacted]")
            if clean != data:
                path.write_bytes(clean)


def _wait(proc, timeout_sec=None):
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()
        raise


def secret_source_paths(root):
    root = resolve_root(root)
    paths = [safe_repository_path(root, item["path"]) for item in manifest_files(root) if item["role"] == "secret-source" and item["available"]]
    default = repository_root(root) / ".env"
    if default.is_file() and default not in paths:
        paths.append(default)
    return paths


def run_script(run_dir, root=None, source_root=None, *, extra_env=None, secret_values=(), timeout_sec=None):
    root = resolve_root(root)
    source_root = root if source_root is None else Path(source_root)
    cfg = read_json(source_root / PROJECT_CONFIG)
    manifest = script_manifest(source_root)
    workdir = manifest["working_dir"].strip().replace("\\", "/").strip("/")
    sandbox = cfg["sandbox"]
    container_name = f"autoexp-{Path(run_dir).name}"
    cmd = ["docker", "run", "--rm", "--name", container_name, "--network", sandbox.get("network", "none"), "--cpus", str(sandbox.get("cpus", "1")), "--memory", sandbox.get("memory", "512m")]
    for path in secret_source_paths(root):
        cmd += ["--env-file", str(path)]
    for key, value in (extra_env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [
        "-e", "AUTOEXP_OUTPUT_DIR=/workspace/run/output", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{source_root.resolve()}:/workspace/source:ro", "-v", f"{Path(run_dir).resolve()}:/workspace/run:rw",
        "-w", f"/workspace/source/{workdir}" if workdir != "." else "/workspace/source",
        manifest.get("image", sandbox["image"]), "sh", "-lc", manifest["command"].replace("${CTX}", "/workspace/run/ctx.json"),
    ]
    logs = Path(run_dir) / "logs"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        return _capture(proc, logs, [*redaction_env_values(root), *secret_values], timeout_sec)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        raise


def docker_ready():
    if shutil.which("docker") is None:
        return False, "required command not found: docker"
    try:
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        return False, f"docker is not usable: {exc.stderr.strip() or 'docker daemon is not reachable'}"
    return True, ""


def app_env(root):
    values = {}
    for path in secret_source_paths(root):
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def local_run_context(run_dir, source_root, root):
    run_dir = Path(run_dir).resolve()
    source_root = Path(source_root).resolve()
    return {"run_dir": str(run_dir), "script_dir": str(source_root), "app_env_path": "", "script_params_path": str(source_root / PARAMS_FILE), "output_dir": str(run_dir / "output"), "logs_dir": str(run_dir / "logs")}


def local_workdir(manifest, run_dir, source_root):
    workdir = manifest["working_dir"].strip().replace("\\", "/")
    if workdir.startswith("/workspace/source/"):
        workdir = workdir.removeprefix("/workspace/source/")
    if workdir.startswith("/workspace/run"):
        return Path(run_dir) / workdir.removeprefix("/workspace/run").lstrip("/")
    target = (Path(source_root) / workdir).resolve()
    if not target.is_relative_to(Path(source_root).resolve()):
        raise ValueError("stage working directory must stay inside the source snapshot")
    return target


def run_script_local(run_dir, root=None, source_root=None, *, extra_env=None, secret_values=(), timeout_sec=None):
    root = resolve_root(root)
    source_root = root if source_root is None else Path(source_root)
    manifest = script_manifest(source_root)
    ctx_path = (Path(run_dir) / "ctx.json").resolve()
    command = manifest["command"].replace("${CTX}", shlex.quote(str(ctx_path)))
    for alias in ("python ", "python3 "):
        if command.startswith(alias):
            command = f"{shlex.quote(sys.executable)} {command.removeprefix(alias)}"
            break
    env = os.environ | app_env(root) | (extra_env or {}) | {"AUTOEXP_RUN_DIR": str(Path(run_dir).resolve()), "AUTOEXP_SCRIPT_DIR": str(source_root.resolve()), "AUTOEXP_OUTPUT_DIR": str((Path(run_dir) / "output").resolve()), "PYTHONDONTWRITEBYTECODE": "1"}
    logs = Path(run_dir) / "logs"
    proc = subprocess.Popen(command, cwd=local_workdir(manifest, run_dir, source_root), env=env, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    return _capture(proc, logs, [*redaction_env_values(root), *secret_values], timeout_sec)


def runner_type(root, source_root=None):
    source_root = Path(root) if source_root is None else Path(source_root)
    requested = read_json(source_root / PROJECT_CONFIG).get("runner", "local")
    if requested not in {"docker", "local"}:
        raise ValueError("runner must be one of: docker, local")
    if requested == "local":
        return "local"
    ok, message = docker_ready()
    if not ok:
        raise ValueError(message)
    return "docker"


def runner_identity(root, runner=None, source_root=None):
    source_root = Path(root) if source_root is None else Path(source_root)
    runner = runner or runner_type(root, source_root)
    if runner == "docker":
        cfg = read_json(source_root / PROJECT_CONFIG)
        manifest = script_manifest(source_root)
        return f"docker:{manifest.get('image', cfg['sandbox']['image'])}"
    return f"{sys.implementation.name}:{platform.python_version()}:{Path(sys.executable).resolve()}"
