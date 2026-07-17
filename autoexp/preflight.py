"""Structured checks that run before an execution row is allocated."""

import shutil
from pathlib import Path

from .runner import docker_ready, runner_identity
from .store import private_git_dir
from .workspace import (
    PROJECT_CONFIG, PROJECT_REPORT_INSTRUCTIONS, STAGE_MANIFEST,
    read_json, repository_root, resolve_root,
)


class PreflightError(ValueError):
    def __init__(self, result):
        self.result = result
        failed = next((item for item in result["checks"] if not item["ok"] and item["required"]), None)
        super().__init__((failed or {}).get("detail") or "execution preflight failed")


def standard_preflight(root=None, source_root=None):
    root = resolve_root(root)
    source_root = Path(source_root) if source_root is not None else None
    checks = []

    def add(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": "" if ok else str(detail), "required": bool(required)})

    def safe_path(base, rel):
        base = Path(base).resolve()
        path = base / rel
        return not path.is_symlink() and path.resolve(strict=False).is_relative_to(base)

    add("repository", repository_root(root).is_dir(), f"registered repository is missing: {repository_root(root)}")
    add("git", shutil.which("git") is not None, "required command not found: git")
    add("private_git", private_git_dir(root).is_dir(), "global private snapshot repository is missing")
    add("global_runs", (root / "runs").is_dir() and safe_path(root, "runs"), "global runs directory is invalid")
    add("snapshot", source_root is not None and source_root.is_dir(), "execution requires a composed source snapshot")
    if source_root is None:
        return {"ok": False, "checks": checks, "runner": None}

    symlinks = [path for path in source_root.rglob("*") if path.is_symlink()]
    add("source_paths", not symlinks, "execution source must not contain symlinks")

    config = None
    try:
        config = read_json(source_root / PROJECT_CONFIG)
        add("config", isinstance(config, dict), f"{PROJECT_CONFIG} must contain a JSON object")
    except (OSError, ValueError) as exc:
        add("config", False, exc)

    manifest = None
    try:
        manifest = read_json(source_root / STAGE_MANIFEST)
        missing = [key for key in ("name", "command", "working_dir", "interface_version") if key not in manifest]
        add("manifest", isinstance(manifest, dict) and not missing, f"{STAGE_MANIFEST} missing: {', '.join(missing)}")
    except (OSError, TypeError, ValueError) as exc:
        add("manifest", False, exc)

    if manifest:
        add("command", bool(str(manifest.get("command", "")).strip()), "stage command is empty")
        workdir = Path(str(manifest.get("working_dir", "")))
        workdir_ok = bool(str(workdir)) and not workdir.is_absolute() and ".." not in workdir.parts
        add("working_dir", workdir_ok and (source_root / workdir).is_dir(), "stage working directory is missing or unsafe")
        entrypoint = str(manifest.get("name", ""))
        add("entrypoint", bool(entrypoint) and safe_path(source_root, entrypoint) and (source_root / entrypoint).is_file(), f"missing entrypoint: {entrypoint}")

    requested = config.get("runner", "local") if isinstance(config, dict) else None
    add("runner", requested in {"local", "docker"}, "runner must be one of: local, docker")
    if isinstance(config, dict):
        add("runtime", isinstance(config.get("runtime", {}), dict), "runtime must be a JSON object")
        if requested == "docker":
            sandbox = config.get("sandbox")
            add("sandbox", isinstance(sandbox, dict), "sandbox must be a JSON object")
            add("image", bool((manifest or {}).get("image") or (sandbox or {}).get("image")), "Docker runner requires an image")
            ok, message = docker_ready()
            add("docker", ok, message)
        add("report_guidance", (source_root / PROJECT_REPORT_INSTRUCTIONS).is_file(), "report guidance is missing from the snapshot")

    ok = all(item["ok"] or not item["required"] for item in checks)
    result = {"ok": ok, "checks": checks, "runner": requested if ok else None}
    if ok:
        try:
            result["runner_identity"] = runner_identity(root, requested, source_root)
        except Exception as exc:
            add("runner_identity", False, exc)
            result.update(ok=False, runner=None)
    return result


def require_preflight(root=None, source_root=None):
    result = standard_preflight(root, source_root)
    if not result["ok"]:
        raise PreflightError(result)
    return result


preflight = standard_preflight
