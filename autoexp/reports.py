import uuid
from pathlib import Path

from .runs import get_run, script_name, source_root_for_run
from .workspace import (
    APP_ENV,
    PARAMS_FILE,
    PROJECT_CONFIG,
    PROJECT_REPORT,
    PROJECT_REPORT_INSTRUCTIONS,
    ensure_within_project,
    read_json,
    resolve_root,
    run_dir_for,
    write_json,
    now,
)


INSIDE_PROJECT_MSG = "report instruction file must stay inside the autoexp project"
REPORT_CONTRACT = """Use `runs/<run_id>/report/report_bundle.json` as the source of truth for report context. The bundle contains the run id, script name, available `.env` variable names, run metadata, and project-relative paths to the report instruction, experiment params, output artifacts, logs, and expected report directory. Read referenced files only as needed.

Write generated report files under `runs/<run_id>/report/`. Prefer `runs/<run_id>/report/report.md` for the main report unless the user asks for a different filename or format. Additional generated images, tables, data files, or appendices may also live in that same report directory.

Do not assume access to secret values. The bundle intentionally includes environment variable names only. Base the report only on the bundled artifacts and the user's request.

Write the final file as ordinary Markdown, not as a patch or diff. Never prefix every line with `+`, `-`, or other diff markers."""

PROJECT_REPORT_CONTRACT = """Synthesize the project as a whole, not one run at a time. Use `project_summary` as the index, inspect milestone evidence and linked per-run reports as needed, and explain the objective, approaches tried, meaningful comparisons, failures, conclusions, and recommended next steps. Distinguish recorded evidence from inference. Write the finished Markdown with `write_project_report`."""


def _safe_path(base, rel, message=INSIDE_PROJECT_MSG):
    base = Path(base).resolve()
    path = base / rel
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        raise ValueError(message)
    return path


def _config(root):
    config = read_json(_safe_path(root, PROJECT_CONFIG))
    if not isinstance(config, dict):
        raise ValueError(f"{PROJECT_CONFIG} must contain a JSON object")
    return config


def app_env_keys(root=None):
    """Names of the variables declared in .env (values are never returned)."""
    root = resolve_root(root)
    path = _safe_path(root, APP_ENV, ".env must stay inside the autoexp project")
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
    configured = _config(root).get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS
    path = ensure_within_project(configured, INSIDE_PROJECT_MSG)
    target = _safe_path(root, path)
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
    if not _safe_path(root, path).is_file():
        raise FileNotFoundError(f"missing report instruction file: {path}")
    config_path = _safe_path(root, PROJECT_CONFIG)
    cfg = _config(root)
    cfg["report_instruction_file"] = path.as_posix()
    write_json(config_path, cfg)
    return path.as_posix()


def write_report_instruction(text, root=None):
    """Overwrite the active report-instruction file's text."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    root = resolve_root(root)
    config_path = _safe_path(root, PROJECT_CONFIG)
    cfg = _config(root)
    path = ensure_within_project(
        cfg.get("report_instruction_file") or PROJECT_REPORT_INSTRUCTIONS,
        INSIDE_PROJECT_MSG,
    )
    target = _safe_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if cfg.get("report_instruction_file") != path.as_posix():
        cfg["report_instruction_file"] = path.as_posix()
        write_json(config_path, cfg)
    return report_instruction(root)


def read_project_report(root=None):
    """Read the one mutable synthesis report for the whole project."""
    root = resolve_root(root)
    path = _safe_path(root, PROJECT_REPORT, "project report must stay inside the autoexp project")
    return {
        "path": PROJECT_REPORT,
        "text": path.read_text(errors="replace") if path.is_file() else "",
        "exists": path.is_file(),
    }


def write_project_report(text, root=None):
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    root = resolve_root(root)
    path = _safe_path(root, PROJECT_REPORT, "project report must stay inside the autoexp project")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return read_project_report(root)


def mark_milestone(*, title, significance, run_id=None, attempt_id=None, actor_name=None, root=None):
    """Attach one concise, append-only finding to a recorded run or research attempt."""
    from .store import db, init_db

    root = resolve_root(root)
    init_db(root)
    if bool(run_id) == bool(attempt_id):
        raise ValueError("provide exactly one of run_id or attempt_id")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    if not isinstance(significance, str) or not significance.strip():
        raise ValueError("significance is required")
    kind, target = ("run", run_id) if run_id else ("attempt", attempt_id)
    conn = db(root)
    if kind == "run":
        exists = conn.execute("select 1 from runs where run_id = ?", (target,)).fetchone()
    else:
        exists = conn.execute(
            "select 1 from research_attempts where attempt_id = ? or contract_id || ':' || attempt_id = ?",
            (target, target),
        ).fetchone()
    if not exists:
        conn.close()
        raise ValueError(f"unknown {kind}: {target}")
    milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "insert into milestones values (?, ?, ?, ?, ?, ?, ?)",
        (milestone_id, kind, target, title.strip()[:120], significance.strip(), actor_name, now()),
    )
    conn.commit()
    conn.close()
    return next(item for item in list_milestones(root) if item["milestone_id"] == milestone_id)


def list_milestones(root=None):
    """Return remarkable evidence with enough linked context for an agent to inspect it."""
    from .runtime import run_report
    from .store import db, init_db

    root = resolve_root(root)
    init_db(root)
    conn = db(root)
    rows = conn.execute("select * from milestones order by created_at desc, rowid desc").fetchall()
    items = []
    for row in rows:
        item = dict(row)
        run_id = item["target_id"] if item["target_kind"] == "run" else None
        if item["target_kind"] == "attempt":
            attempt = conn.execute(
                """select contract_id, attempt_id, hypothesis, score, verdict, improvement, run_id
                   from research_attempts
                   where attempt_id = ? or contract_id || ':' || attempt_id = ?
                   order by rowid desc limit 1""",
                (item["target_id"], item["target_id"]),
            ).fetchone()
            item["attempt"] = dict(attempt) if attempt else None
            run_id = attempt["run_id"] if attempt else None
        item["run_id"] = run_id
        if run_id:
            report = run_report(run_id, root)
            item["report_path"] = report.get("path") or ""
            item["report_excerpt"] = report.get("text", "")[:4000]
        items.append(item)
    conn.close()
    return items


def project_summary(root=None, limit=20):
    """Return the compact project-level evidence index used for final synthesis."""
    from .runtime import list_runs
    from .workspace import project_entry, project_mode

    root = resolve_root(root)
    mode = project_mode(root)
    result = {
        "project": project_entry(root),
        "mode": mode,
        "project_report": read_project_report(root),
        "milestones": list_milestones(root),
        "runs": list_runs(limit, root),
        "report_contract": PROJECT_REPORT_CONTRACT,
    }
    if mode == "autoresearch":
        from .autoresearch import for_project

        state = for_project(root).state()
        result["autoresearch"] = {
            "objective": state["objective"],
            "contract": state["contract"],
            "experiments": state["experiments"],
            "loop": state["loop"],
        }
    return result


def write_report_bundle(run_id, root=None):
    """Write runs/<id>/report/report_bundle.json: pointers a reporter needs in one place."""
    root = resolve_root(root)
    run = get_run(run_id, root)
    run_dir = run_dir_for(run, root)
    if not run_dir.exists():
        raise FileNotFoundError(f"missing run directory: {run_dir.relative_to(root)}")
    source_root = source_root_for_run(run, root)
    params_path = _safe_path(source_root, PARAMS_FILE, "run params path is unsafe")
    report_dir = _safe_path(run_dir, "report", "run report directory is unsafe")
    bundle_path = _safe_path(report_dir, "report_bundle.json", "report bundle path is unsafe")
    report_dir.mkdir(parents=True, exist_ok=True)
    from .artifacts import list_artifacts

    indexed = list_artifacts(run_id, root)
    run_prefix = run_dir.relative_to(root).as_posix()
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
            "output": [
                f"{run_prefix}/{item['path']}"
                for item in indexed
                if item["category"] == "output"
            ],
            "logs": [
                f"{run_prefix}/{item['path']}"
                for item in indexed
                if item["category"] == "log"
            ],
        },
    }
    write_json(bundle_path, bundle)
    return bundle
