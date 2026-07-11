"""Structured checks that run before an execution row is allocated."""

import shutil
from pathlib import Path

from .runner import docker_ready, runner_identity
from .workspace import (
    APP_ENV,
    PROJECT_CONFIG,
    PROJECT_REPORT_INSTRUCTIONS,
    ensure_within_project,
    is_project_root,
    read_json,
    resolve_root,
)


class PreflightError(ValueError):
    def __init__(self, result):
        self.result = result
        failed = next((item for item in result["checks"] if not item["ok"]), None)
        message = failed["detail"] or failed["name"] if failed else "execution preflight failed"
        super().__init__(message)


def standard_preflight(root=None, source_root=None):
    root = resolve_root(root)
    source_root = root if source_root is None else Path(source_root)
    checks = []

    def add(name, ok, detail="", required=True):
        checks.append({
            "name": name,
            "ok": bool(ok),
            "detail": str(detail),
            "required": bool(required),
        })

    def safe_path(base, rel):
        base = Path(base).resolve()
        path = base / rel
        return not path.is_symlink() and path.resolve(strict=False).is_relative_to(base)

    add("project", is_project_root(root), f"invalid Autoexp project: {root}")
    add("git", shutil.which("git") is not None, "required command not found: git")
    git_dir = root / ".autoexp" / "git"
    add(
        "private_git",
        git_dir.is_dir() and safe_path(root, ".autoexp/git"),
        f"{root} is missing its Autoexp git repository",
    )
    add(
        "runs_directory",
        (root / "runs").is_dir() and safe_path(root, "runs"),
        "runs directory must stay inside the autoexp project",
    )
    add(
        "app_env",
        (
            not (root / APP_ENV).exists()
            and not (root / APP_ENV).is_symlink()
        ) or safe_path(root, APP_ENV),
        "app.env must stay inside the autoexp project",
    )
    script_dir = source_root / "script"
    source_symlinks = (
        [path for path in script_dir.rglob("*") if path.is_symlink()]
        if script_dir.is_dir() and not script_dir.is_symlink()
        else [script_dir]
    )
    add(
        "source_paths",
        safe_path(source_root, "script")
        and safe_path(source_root, PROJECT_CONFIG)
        and safe_path(source_root, ".gitignore")
        and not source_symlinks,
        "execution source must not contain symlinks or escape its snapshot",
    )

    config = None
    try:
        if not safe_path(source_root, PROJECT_CONFIG):
            raise ValueError("autoexp.json must stay inside the source snapshot")
        config = read_json(source_root / PROJECT_CONFIG)
        add("config", isinstance(config, dict), "autoexp.json must contain a JSON object")
    except (OSError, ValueError) as exc:
        add("config", False, exc)

    manifest = None
    try:
        if not safe_path(source_root, "script/stage.json"):
            raise ValueError("script/stage.json must stay inside the source snapshot")
        manifest = read_json(source_root / "script" / "stage.json")
        if not isinstance(manifest, dict):
            add("manifest", False, "script/stage.json must contain a JSON object")
            manifest = None
        else:
            missing = [
                key for key in ("name", "command", "working_dir", "interface_version")
                if key not in manifest
            ]
            add("manifest", not missing, f"script/stage.json missing: {', '.join(missing)}")
    except (OSError, TypeError, ValueError) as exc:
        add("manifest", False, exc)

    if manifest is not None:
        add("command", bool(str(manifest.get("command", "")).strip()), "stage command is empty")
        workdir = str(manifest.get("working_dir", "")).strip()
        add("working_dir", bool(workdir), "stage working_dir is empty")

    requested = config.get("runner", "local") if isinstance(config, dict) else None
    add("runner", requested in {"local", "docker"}, "runner must be one of: local, docker")
    if isinstance(config, dict):
        add("runtime", isinstance(config.get("runtime", {}), dict), "runtime must be a JSON object")
        if requested == "docker":
            sandbox = config.get("sandbox")
            add("sandbox", isinstance(sandbox, dict), "sandbox must be a JSON object")
            if isinstance(sandbox, dict):
                image = (manifest or {}).get("image") or sandbox.get("image")
                add("image", bool(image), "Docker runner requires an image")
            ok, message = docker_ready()
            add("docker", ok, message)

        try:
            report_path = ensure_within_project(
                config.get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS,
                "report instruction file must stay inside the autoexp project",
            )
            add(
                "report_instruction",
                safe_path(source_root, report_path) and (source_root / report_path).is_file(),
                f"missing report instruction file: {report_path}",
            )
        except (TypeError, ValueError) as exc:
            add("report_instruction", False, exc)

    ok = all(item["ok"] or not item["required"] for item in checks)
    result = {"ok": ok, "checks": checks, "runner": requested if ok else None}
    if ok:
        try:
            result["runner_identity"] = runner_identity(root, requested, source_root)
        except Exception as exc:
            add("runner_identity", False, exc)
            result["ok"] = False
            result["runner"] = None
    return result


def require_preflight(root=None, source_root=None):
    result = standard_preflight(root, source_root)
    if not result["ok"]:
        raise PreflightError(result)
    return result


preflight = standard_preflight
