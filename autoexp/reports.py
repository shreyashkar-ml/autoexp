from pathlib import Path

from .runs import get_run, script_name, source_root_for_run
from .workspace import (
    APP_ENV,
    PROJECT_CONFIG,
    PROJECT_REPORT_INSTRUCTIONS,
    ensure_within_project,
    read_json,
    resolve_root,
    run_dir_for,
    write_json,
)


INSIDE_PROJECT_MSG = "report instruction file must stay inside the autoexp project"
REPORT_CONTRACT = """Use `runs/<run_id>/report/report_bundle.json` as the source of truth for report context. The bundle contains the run id, script name, available `app.env` variable names, run metadata, and project-relative paths to the report instruction, script params, output artifacts, logs, and expected report directory. Read referenced files only as needed.

Write generated report files under `runs/<run_id>/report/`. Prefer `runs/<run_id>/report/report.md` for the main report unless the user asks for a different filename or format. Additional generated images, tables, data files, or appendices may also live in that same report directory.

Do not assume access to secret values. The bundle intentionally includes environment variable names only. Base the report only on the bundled artifacts and the user's request."""


def app_env_keys(root=None):
    """Names of the variables declared in app.env (values are never returned)."""
    root = resolve_root(root)
    path = root / APP_ENV
    if not path.exists():
        return []
    keys = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def report_instruction(root=None):
    """Read the editable, project-specific report guidance."""
    root = resolve_root(root)
    configured = read_json(root / PROJECT_CONFIG).get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS
    path = ensure_within_project(configured, INSIDE_PROJECT_MSG)
    target = root / path
    if not target.is_file():
        raise FileNotFoundError(f"missing report instruction file: {configured}")
    text = target.read_text().rstrip()
    if text.endswith(REPORT_CONTRACT):
        text = text.removesuffix(REPORT_CONTRACT).rstrip()
    return {"source": target.relative_to(root).as_posix(), "text": text + "\n"}


def report_generation_instruction(root=None):
    """Join project guidance with Autoexp's invariant report contract."""
    instruction = report_instruction(root)
    return {**instruction, "text": f"{instruction['text'].rstrip()}\n\n{REPORT_CONTRACT}\n"}


def set_report_instruction(path, root=None):
    """Point the project at a different report-instruction file."""
    root = resolve_root(root)
    path = Path(path)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise ValueError(INSIDE_PROJECT_MSG)
    path = ensure_within_project(path, INSIDE_PROJECT_MSG)
    if not (root / path).is_file():
        raise FileNotFoundError(f"missing report instruction file: {path}")
    cfg = read_json(root / PROJECT_CONFIG)
    cfg["report_instruction_file"] = path.as_posix()
    write_json(root / PROJECT_CONFIG, cfg)
    return path.as_posix()


def write_report_instruction(text, root=None):
    """Overwrite the active report-instruction file's text."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    root = resolve_root(root)
    cfg = read_json(root / PROJECT_CONFIG)
    path = ensure_within_project(
        cfg.get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS,
        INSIDE_PROJECT_MSG,
    )
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if cfg.get("report_instruction_file") != path.as_posix():
        cfg["report_instruction_file"] = path.as_posix()
        write_json(root / PROJECT_CONFIG, cfg)
    return report_instruction(root)


def artifact_files(base):
    """Every file under base, as {path, text} relative to base."""
    base = Path(base)
    if not base.exists():
        return []
    return [
        {"path": item.relative_to(base).as_posix(), "text": item.read_text(errors="replace")}
        for item in sorted(base.rglob("*"))
        if item.is_file()
    ]


def artifact_paths(base, root):
    """Every file under base, as project-relative path strings."""
    base = Path(base)
    if not base.exists():
        return []
    return [
        item.relative_to(root).as_posix()
        for item in sorted(base.rglob("*"))
        if item.is_file()
    ]


def write_report_bundle(run_id, root=None):
    """Write runs/<id>/report/report_bundle.json: pointers a reporter needs in one place."""
    root = resolve_root(root)
    run = get_run(run_id, root)
    run_dir = run_dir_for(run, root)
    if not run_dir.exists():
        raise FileNotFoundError(f"missing run directory: {run_dir.relative_to(root)}")
    source_root = source_root_for_run(run, root)
    params_path = source_root / "script" / "params.json"
    report_dir = run_dir / "report"
    bundle_path = report_dir / "report_bundle.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    bundle = {
        "bundle_path": bundle_path.relative_to(root).as_posix(),
        "run_id": run_id,
        "script": run.get("script_name") or script_name(run_id, source_root),
        "report": run.get("report_path") or "",
        "report_dir": report_dir.relative_to(root).as_posix(),
        "app_env_keys": app_env_keys(root),
        "instruction": report_instruction(root)["source"],
        "script_params": params_path.relative_to(root).as_posix() if params_path.exists() else "",
        "run": {key: run.get(key) for key in ("status", "created_at", "output_hash", "capsule_hash")},
        "artifacts": {
            "output": artifact_paths(run_dir / "output", root),
            "logs": artifact_paths(run_dir / "logs", root),
        },
    }
    write_json(bundle_path, bundle)
    return bundle
