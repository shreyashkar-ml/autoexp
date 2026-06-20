import json
import subprocess
import sys
import uuid
from pathlib import Path

from .reports import artifact_files, report_instruction, write_report_bundle
from .runner import RUN_CONTEXT, compute_hashes, docker_ready, hash_run_output
from .runs import copy_run_source, get_run, new_run_id, restore_run_state, run_stage_commit, source_root_for_run
from .store import (
    autoexp_git,
    current_autoexp_commit,
    db,
    init_db,
    insert_run,
    require_autoexp_git_repo,
)
from .workspace import APP_ENV, PROJECT_CONFIG, PROJECT_INSTRUCTIONS, is_project_root, now, project_entry, project_root, read_json, script_manifest, source_paths, write_json


def json_value(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value) if value is not None else None
    except json.JSONDecodeError:
        return value


def report_path_for_run(run, root):
    root = Path(root)
    path = run.get("report_path")
    if path and (root / path).is_file():
        return path

    report_dir = root / (run.get("run_dir") or f"runs/{run['run_id']}") / "report"
    for candidate in ("report.md", "report.txt", "index.md"):
        if (report_dir / candidate).is_file():
            return (report_dir / candidate).relative_to(root).as_posix()

    for item in sorted(report_dir.glob("*")):
        if item.is_file() and item.name != "report_bundle.json":
            return item.relative_to(root).as_posix()

    return ""


def output_files_for_run(run, root):
    output_dir = Path(root) / (run.get("run_dir") or f"runs/{run['run_id']}") / "output"
    if not output_dir.is_dir():
        return []
    return [
        item.relative_to(output_dir).as_posix()
        for item in sorted(output_dir.rglob("*"))
        if item.is_file()
    ]


def run_row(row, root):
    run = dict(row)
    run["stage_status"] = json_value(run["stage_status"])
    run["report_path"] = report_path_for_run(run, root)
    run["output_files"] = output_files_for_run(run, root)
    return run


def list_runs(limit=20, root=None):
    root = project_root() if root is None else Path(root)
    conn = db(root)
    rows = conn.execute(
        "select * from runs order by created_at desc limit ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [run_row(row, root) for row in rows]


def read_script_params(root=None):
    root = project_root() if root is None else Path(root)
    schema_path = root / "script" / "params.schema.json"
    params_path = root / "script" / "params.json"
    return {
        "schema": read_json(schema_path) if schema_path.exists() else None,
        "params": read_json(params_path) if params_path.exists() else None,
    }


def write_script_params(params, root=None):
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")
    root = project_root() if root is None else Path(root)
    write_json(root / "script" / "params.json", params)
    return read_script_params(root)


def run_source(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run = get_run(run_id, root)
    source_root = source_root_for_run(run, root)
    base = source_root / "script"
    files = []

    for item in sorted(base.rglob("*")):
        rel = item.relative_to(base).as_posix()
        if item.is_file() and rel not in {"stage.json", "params.json", "params.schema.json"}:
            files.append({
                "path": rel,
                "text": item.read_text(errors="replace"),
            })

    selected = next((item["path"] for item in files if item["path"] == (run.get("script_name") or "")), "")
    if not selected:
        selected = files[0]["path"] if files else ""
    return {"run_id": run_id, "script": run.get("script_name"), "selected": selected, "files": files}


def run_report(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run = get_run(run_id, root)
    path = report_path_for_run(run, root)

    if not path:
        return {"run_id": run_id, "path": "", "text": ""}

    return {"run_id": run_id, "path": path, "text": (root / path).read_text(errors="replace")}


def read_output_files(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run = get_run(run_id, root)
    run_dir = root / (run.get("run_dir") or f"runs/{run_id}")
    return {"run_id": run_id, "files": artifact_files(run_dir / "output")}


def read_logs(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run = get_run(run_id, root)
    run_dir = root / (run.get("run_dir") or f"runs/{run_id}")
    return {"run_id": run_id, "files": artifact_files(run_dir / "logs")}


def read_report_bundle(run_id, root=None):
    root = project_root() if root is None else Path(root)
    path = root / "runs" / run_id / "report" / "report_bundle.json"
    if not path.is_file():
        write_report_bundle(run_id, root)
    return read_json(path)


def script_version_name(root, rel):
    rel = Path(rel)
    parent = rel.parent
    stem = rel.stem
    suffix = rel.suffix
    base = stem
    if "_v" in stem:
        prefix, version = stem.rsplit("_v", 1)
        if version.isdigit():
            base = prefix

    highest = 1
    for script in (Path(root) / "runs").glob("*/script/*"):
        if not script.is_file() or script.parent.parent.name.startswith("."):
            continue
        existing = script.relative_to(script.parent.parent / "script")
        if existing.parent != parent or existing.suffix != suffix:
            continue
        if existing.stem == base:
            highest = max(highest, 1)
        elif existing.stem.startswith(f"{base}_v"):
            version = existing.stem.removeprefix(f"{base}_v")
            if version.isdigit():
                highest = max(highest, int(version))

    return (parent / f"{base}_v{highest + 1}{suffix}").as_posix()


def safe_script_path(path, label):
    rel = Path(path)
    if rel.is_absolute() or ".." in rel.parts or not rel.name:
        raise ValueError(f"{label} must stay inside script/")
    return rel


def save_script_file(path, text, root=None, source_run_id=None, save_as=None):
    root = project_root() if root is None else Path(root)
    rel = safe_script_path(path, "path")
    saved_rel = safe_script_path(save_as, "save_as") if save_as else Path(script_version_name(root, rel))

    source_run = get_run(source_run_id, root) if source_run_id else None
    source_root = source_root_for_run(source_run, root) if source_run else root
    if not (source_root / "script" / rel).is_file():
        raise ValueError(f"unknown script file: {rel.as_posix()}")

    tmp = root / "runs" / f".tmp_ui_edit_{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    for name in ("output", "logs", "report"):
        (tmp / name).mkdir()
    copy_run_source(source_root, tmp)
    if saved_rel != rel:
        (tmp / "script" / rel).unlink(missing_ok=True)
    edited = tmp / "script" / saved_rel
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text(text)

    manifest_path = tmp / "script" / "stage.json"
    manifest = read_json(manifest_path)
    command = manifest.get("command", "")
    for candidate in (rel.as_posix(), rel.name, str(manifest.get("name") or "")):
        if candidate and candidate in command:
            command = command.replace(candidate, saved_rel.as_posix(), 1)
            break
    manifest["name"] = saved_rel.as_posix()
    manifest["command"] = command
    write_json(manifest_path, manifest)

    hashes = compute_hashes(tmp)
    run_id, _ = new_run_id(hashes, root)
    run_dir = root / "runs" / run_id
    tmp.rename(run_dir)
    meta = {
        "run_id": run_id,
        "run_dir": f"runs/{run_id}",
        "report_path": "",
        "output_hash": hash_run_output(run_dir),
        "script_name": saved_rel.as_posix(),
        **hashes,
        "stage_commit": current_autoexp_commit(root),
        "status": "edited",
        "stage_status": {"script": "edited"},
        "created_at": now(),
    }
    write_json(run_dir / "ctx.json", RUN_CONTEXT)
    write_json(run_dir / "run.json", meta)
    insert_run(meta, root=root)
    write_report_bundle(run_id, root=root)
    return {"path": saved_rel.as_posix(), "run": run_row(meta, root)}


def workspace(root=None):
    root = project_root() if root is None else Path(root)
    return {"root": str(root), "project": project_entry(root)}


def restore(run_id, root=None):
    root = project_root() if root is None else Path(root)
    run, commit = restore_run_state(run_id, root)
    return {"run_id": run_id, "stage_commit": commit, "script_name": run.get("script_name")}


def diff_runs(run_a, run_b, root=None):
    root = project_root() if root is None else Path(root)
    a = get_run(run_a, root)
    b = get_run(run_b, root)
    return autoexp_git(["diff", run_stage_commit(a), run_stage_commit(b), "--", *source_paths(root)], root=root, capture=True, check=False)


def run_autoexp(run_id=None, root=None):
    root = project_root() if root is None else Path(root)
    proc = subprocess.run(
        [sys.executable, "-m", "autoexp", "run", *([run_id] if run_id else [])],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "runs": list_runs(root=root)}


def doctor(root=None):
    root = project_root() if root is None else Path(root)
    checks = []

    def add(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": bool(required)})

    add("project_root", is_project_root(root), str(root))
    add("autoexp.md", (root / PROJECT_INSTRUCTIONS).is_file())
    add("script/stage.json", (root / "script" / "stage.json").is_file())
    runner = "local"
    try:
        runner = read_json(root / PROJECT_CONFIG).get("runner", "local")
        add("runner", runner in {"docker", "local"}, runner)
    except Exception as exc:
        add("runner", False, str(exc))
    try:
        manifest = script_manifest(root)
        missing = [key for key in ("name", "command", "working_dir", "interface_version") if key not in manifest]
        add("stage_manifest_keys", not missing, ", ".join(missing))
        detail = "" if "${CTX}" in manifest.get("command", "") else "command does not use ${CTX}; scripts can still use AUTOEXP_OUTPUT_DIR"
        add("stage_command_context", True, detail)
    except SystemExit as exc:
        add("stage_manifest_keys", False, str(exc))

    init_db(root)
    add("index.sqlite", (root / "index.sqlite").is_file())
    add("private_git", (root / ".autoexp" / "git").is_dir())
    add("app.env_ignored", APP_ENV in (root / ".gitignore").read_text() if (root / ".gitignore").is_file() else False)
    try:
        require_autoexp_git_repo(root)
        add("private_git_root", True)
    except SystemExit as exc:
        add("private_git_root", False, str(exc))
    try:
        report_instruction(root)
        add("report_instruction", True)
    except Exception as exc:
        add("report_instruction", False, str(exc))
    if runner == "local":
        add("docker", True, "not required for local runner")
    else:
        ok, message = docker_ready()
        add("docker", ok, "" if ok else message, required=runner == "docker")

    return {"root": str(root), "ok": all(item["ok"] or not item.get("required", True) for item in checks), "checks": checks}
