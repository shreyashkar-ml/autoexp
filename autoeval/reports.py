from pathlib import Path

from .runs import get_run, script_name, source_root_for_run
from .workspace import APP_ENV, PROJECT_CONFIG, PROJECT_REPORT_INSTRUCTIONS, die, project_root, read_json, write_json


def app_env_keys(root=None):
    root = project_root() if root is None else Path(root)
    path, keys = root / APP_ENV, []
    if not path.exists():
        return keys
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def report_instruction(root=None):
    root = project_root() if root is None else Path(root)
    configured = read_json(root / PROJECT_CONFIG).get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS
    path = Path(configured)
    if path.is_absolute() or ".." in path.parts or not path.name:
        raise ValueError("report instruction file must stay inside the autoeval project")
    target = root / path
    if not target.is_file():
        raise FileNotFoundError(f"missing report instruction file: {configured}")
    return {"source": target.relative_to(root).as_posix(), "text": target.read_text()}


def set_report_instruction(path, root=None):
    root, path = project_root() if root is None else Path(root), Path(path)
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
        {"path": item.relative_to(base).as_posix(), "text": item.read_text(errors="replace")}
        for item in sorted(base.rglob("*")) if item.is_file()
    ]


def artifact_paths(base, root):
    base = Path(base)
    return [item.relative_to(root).as_posix() for item in sorted(base.rglob("*")) if item.is_file()] if base.exists() else []


def write_report_bundle(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run = get_run(run_id, root)
    run_dir = root / (run.get("run_dir") or f"runs/{run_id}")
    if not run_dir.exists():
        die(f"missing run directory: {run_dir.relative_to(root)}")
    source_root = source_root_for_run(run, root)
    params_path = source_root / "script" / "params.json"
    report_dir, bundle_path = run_dir / "report", run_dir / "report" / "report_bundle.json"
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
