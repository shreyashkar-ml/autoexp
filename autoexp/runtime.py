import json
import tempfile
from collections import Counter
from pathlib import Path

from .artifacts import (
    artifact_content,
    index_report_artifacts,
    list_artifacts,
    read_log,
)
from .provenance import (
    create_trigger,
    get_trigger,
    reproducibility_summary,
    reproduction_state,
)
from .reports import report_instruction, write_report_bundle
from .runner import docker_ready
from .runs import get_run, source_root_for_run, restore_run_state, run_stage_commit
from .snapshots import (
    capture_source_tree,
    capture_workspace,
    diff_snapshots,
    get_snapshot,
    materialize_snapshot,
)
from .store import (
    autoexp_git,
    db,
    init_db,
    require_autoexp_git_repo,
)
from .workspace import (
    APP_ENV,
    PROJECT_CONFIG,
    PROJECT_INSTRUCTIONS,
    ensure_within_project,
    is_project_root,
    project_entry,
    project_mode,
    read_json,
    resolve_root,
    script_manifest,
    source_paths,
    write_json,
)


# This module is the shared "verbs" layer: every action the CLI, MCP server, and
# HTTP server expose ultimately calls one of these functions with a project root.


def json_value(value):
    """Best-effort decode of a JSON-encoded column; pass through anything else."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


# ======================================================================
#  Reading runs
# ======================================================================

def report_path_for_run(run, root):
    """The primary indexed report path, retaining the legacy project-relative shape."""
    path = run.get("report_path")
    if path:
        return path
    reports = list_artifacts(run["run_id"], root=root, category="report")
    artifact = _primary_report(reports)
    return f"{run['run_dir'].rstrip('/')}/{artifact['path']}" if artifact else ""


def output_files_for_run(run, root):
    return [item["path"] for item in list_artifacts(run["run_id"], root=root, category="output")]


def _primary_report(artifacts):
    preferred = ("report/report.md", "report/report.txt", "report/index.md")
    by_path = {item["path"]: item for item in artifacts}
    return next(
        (by_path[path] for path in preferred if path in by_path),
        next((item for item in artifacts if not item["path"].endswith("report_bundle.json")), None),
    )


def run_row(row, root):
    """Decorate a stored run row with its resolved report path and output listing."""
    run = dict(row)
    run["stage_status"] = json_value(run.get("stage_status"))
    run["report_path"] = report_path_for_run(run, root)
    run["output_files"] = output_files_for_run(run, root)
    return run


def list_runs(limit=20, root=None):
    root = resolve_root(root)
    limit = max(1, min(int(limit), 200))
    conn = db(root)
    rows = conn.execute(
        "select * from runs order by created_at desc, rowid desc limit ?", (limit,)
    ).fetchall()
    conn.close()
    return [run_row(row, root) for row in rows]


def read_script_params(root=None):
    root = resolve_root(root)
    schema_path = _safe_script_path(root / "script", "params.schema.json")
    params_path = _safe_script_path(root / "script", "params.json")
    return {
        "schema": read_json(schema_path) if schema_path.exists() else None,
        "params": read_json(params_path) if params_path.exists() else None,
    }


def write_script_params(params, root=None, *, trigger_kind=None, actor_name=None):
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")
    root = resolve_root(root)
    init_db(root)
    write_json(_safe_script_path(root / "script", "params.json"), params)
    trigger = create_trigger(
        trigger_kind,
        root=root,
        actor_name=actor_name,
        metadata={"operation": "params_update"},
    ) if trigger_kind else None
    snapshot = capture_workspace(
        root,
        label="Params update",
        created_by_trigger_id=trigger["trigger_id"] if trigger else None,
    )
    return {**read_script_params(root), "snapshot": snapshot}


def _safe_script_path(script_root, rel):
    script_root = Path(script_root)
    if script_root.is_symlink():
        raise ValueError("script directory must not be a symlink")
    resolved_root = script_root.resolve()
    path = script_root / rel
    cursor = script_root
    for part in Path(rel).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"script path must not contain symlinks: {rel}")
    if not path.resolve(strict=False).is_relative_to(resolved_root):
        raise ValueError(f"script path must stay inside script/: {rel}")
    return path


def run_source(run_id, root=None):
    """List a run's source files (excluding manifest/params), with one selected."""
    root = resolve_root(root)
    run = get_run(run_id, root)
    def read_files(source_root):
        base = source_root / "script"
        if base.is_symlink() or not base.is_dir():
            raise ValueError("snapshot script directory is invalid")
        skip = {"stage.json", "params.json", "params.schema.json"}
        files = []
        for item in sorted(base.rglob("*")):
            rel = item.relative_to(base)
            if item.is_symlink() or not item.is_file() or rel.as_posix() in skip:
                continue
            safe = _safe_script_path(base, rel)
            files.append({"path": rel.as_posix(), "text": safe.read_text(errors="replace")})
        return files

    if run.get("source_snapshot_id"):
        with tempfile.TemporaryDirectory(prefix="autoexp-run-source-") as tmp:
            source_root = Path(tmp)
            materialize_snapshot(run["source_snapshot_id"], source_root, root)
            files = read_files(source_root)
    else:
        files = read_files(source_root_for_run(run, root))

    wanted = run.get("script_name") or ""
    selected = next((f["path"] for f in files if f["path"] == wanted), "")
    if not selected and files:
        selected = files[0]["path"]
    return {"run_id": run_id, "script": run.get("script_name"), "selected": selected, "files": files}


def run_report(run_id, root=None):
    root = resolve_root(root)
    index_report_artifacts(run_id, root)
    artifact = _primary_report(list_artifacts(run_id, root=root, category="report"))
    if not artifact:
        return {"run_id": run_id, "path": "", "artifact": None, "text": ""}
    artifact, content = artifact_content(run_id, artifact["artifact_id"], root)
    return {
        "run_id": run_id,
        "path": artifact["path"],
        "artifact": artifact,
        "text": content.decode(errors="replace"),
    }


def read_output_files(run_id, root=None):
    root = resolve_root(root)
    return {"run_id": run_id, "files": list_artifacts(run_id, root=root, category="output")}


def read_logs(run_id, root=None):
    root = resolve_root(root)
    streams = [read_log(run_id, stream, root=root) for stream in ("stdout", "stderr")]
    return {
        "run_id": run_id,
        "streams": streams,
        "files": [
            {"path": f"script.{stream['stream']}.log", "text": stream["text"]}
            for stream in streams
        ],
    }


def read_report_bundle(run_id, root=None):
    root = resolve_root(root)
    return write_report_bundle(run_id, root)


def _run_summary(run):
    if not run:
        return None
    keys = (
        "run_id", "status", "source_snapshot_id", "parent_run_id", "created_at",
        "started_at", "ended_at", "output_hash", "capsule_hash",
    )
    return {key: run.get(key) for key in keys}


def run_overview(run_id, root=None):
    """Aggregate the indexed evidence needed by the run Overview."""
    root = resolve_root(root)
    run = run_row(get_run(run_id, root), root)
    snapshot = get_snapshot(run["source_snapshot_id"], root) if run.get("source_snapshot_id") else None
    parent = get_run(run["parent_run_id"], root) if run.get("parent_run_id") else None
    trigger = get_trigger(run["trigger_id"], root) if run.get("trigger_id") else None
    artifacts = list_artifacts(run_id, root=root)
    counts = Counter(item["category"] for item in artifacts)
    return {
        "run": run,
        "source_snapshot": snapshot,
        "parent_run": _run_summary(parent),
        "trigger": trigger,
        "reproducibility": reproducibility_summary(run_id, root),
        "artifact_summary": {
            "total": len(artifacts),
            "output": counts["output"],
            "log": counts["log"],
            "report": counts["report"],
            "by_category": dict(counts),
            "artifacts": artifacts,
        },
        "reproduction": reproduction_state(run_id, root),
    }


def run_diff(run_id, root=None, *, base_run_id=None, base_snapshot_id=None):
    """Compare a run's pinned snapshot with an explicit or lineage-derived base."""
    root = resolve_root(root)
    run = get_run(run_id, root)
    snapshot_id = run.get("source_snapshot_id")
    if not snapshot_id:
        raise ValueError(f"run {run_id} has no source snapshot")
    snapshot = get_snapshot(snapshot_id, root)

    if base_run_id:
        base_snapshot_id = get_run(base_run_id, root).get("source_snapshot_id")
    elif not base_snapshot_id and run.get("parent_run_id"):
        base_run_id = run["parent_run_id"]
        base_snapshot_id = get_run(base_run_id, root).get("source_snapshot_id")
    elif not base_snapshot_id:
        base_snapshot_id = snapshot.get("parent_snapshot_id")

    if not base_snapshot_id:
        return {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "base_run_id": base_run_id,
            "base_snapshot_id": None,
            "available": False,
            "changed_files": [],
            "changes": {
                "code": False,
                "params": False,
                "manifest": False,
                "runtime": False,
            },
            "diff": "",
        }

    base = get_snapshot(base_snapshot_id, root)
    diff = diff_snapshots(base_snapshot_id, snapshot_id, root)
    changed_files = autoexp_git(
        ["diff", "--name-only", base["git_commit"], snapshot["git_commit"], "--", *source_paths(root)],
        root=root,
        capture=True,
        check=False,
    ).splitlines()
    return {
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "base_run_id": base_run_id,
        "base_snapshot_id": base_snapshot_id,
        "available": True,
        "changed_files": changed_files,
        "changes": {
            "code": base["script_hash"] != snapshot["script_hash"],
            "params": base["params_hash"] != snapshot["params_hash"],
            "manifest": base["manifest_hash"] != snapshot["manifest_hash"],
            "runtime": base["runtime_config_hash"] != snapshot["runtime_config_hash"],
        },
        "diff": diff,
    }


# ======================================================================
#  Editing scripts into new run snapshots
# ======================================================================

def next_script_version(source_root, rel):
    """Next free `name_vN` for a script path within one source snapshot."""
    rel = Path(rel)
    parent = rel.parent
    suffix = rel.suffix
    base = rel.stem
    if "_v" in base:
        prefix, version = base.rsplit("_v", 1)
        if version.isdigit():
            base = prefix

    highest = 1  # a bare `base` (no suffix) counts as version 1
    script_root = Path(source_root) / "script"
    if script_root.is_symlink() or not script_root.is_dir():
        raise ValueError("snapshot script directory is invalid")
    for script in script_root.rglob("*"):
        if script.is_symlink() or not script.is_file():
            continue
        existing = script.relative_to(script_root)
        _safe_script_path(script_root, existing)
        if existing.parent != parent or existing.suffix != suffix:
            continue
        if existing.stem.startswith(f"{base}_v"):
            version = existing.stem.removeprefix(f"{base}_v")
            if version.isdigit():
                highest = max(highest, int(version))

    return (parent / f"{base}_v{highest + 1}{suffix}").as_posix()


def _source_snapshot(root, source_run_id=None, source_snapshot_id=None, trigger_id=None):
    if source_snapshot_id:
        return get_snapshot(source_snapshot_id, root)
    if not source_run_id:
        return capture_workspace(
            root,
            label="Script edit base",
            created_by_trigger_id=trigger_id,
        )

    run = get_run(source_run_id, root)
    if run.get("source_snapshot_id"):
        return get_snapshot(run["source_snapshot_id"], root)

    snapshot = capture_source_tree(
        source_root_for_run(run, root),
        root,
        parent_commit=run_stage_commit(run),
        label=f"Recovered source for run {source_run_id}",
        created_by_trigger_id=trigger_id,
    )
    conn = db(root)
    conn.execute(
        "update runs set source_snapshot_id = ? where run_id = ?",
        (snapshot["snapshot_id"], source_run_id),
    )
    conn.commit()
    conn.close()
    return snapshot


def save_script_file(
    path,
    text,
    root=None,
    source_run_id=None,
    save_as=None,
    source_snapshot_id=None,
    trigger_kind=None,
    actor_name=None,
):
    """Derive an edited source snapshot without creating an execution row."""
    root = resolve_root(root)
    init_db(root)
    rel = ensure_within_project(path, "path must stay inside script/")
    if project_mode(root) == "autoresearch":
        from .autoresearch import (
            ResearchConfig,
            ensure_research_file_editable,
            for_project as research_for_project,
        )

        ensure_research_file_editable(root, rel)
        research_path = f"script/{rel.as_posix()}"
        config = ResearchConfig.load(root)
        if (
            config.role_of(research_path) in {"human", "agent"}
            and not source_run_id
            and not source_snapshot_id
            and not save_as
        ):
            return research_for_project(root).save_file(research_path, text)
    trigger = create_trigger(
        trigger_kind,
        root=root,
        actor_name=actor_name,
        metadata={"operation": "script_edit"},
    ) if trigger_kind else None
    trigger_id = trigger["trigger_id"] if trigger else None
    base = _source_snapshot(root, source_run_id, source_snapshot_id, trigger_id)

    with tempfile.TemporaryDirectory(prefix="autoexp-script-edit-") as tmp:
        source_root = Path(tmp)
        materialize_snapshot(base["snapshot_id"], source_root, root)
        saved_rel = (
            ensure_within_project(save_as, "save_as must stay inside script/")
            if save_as
            else Path(next_script_version(source_root, rel))
        )
        script_root = source_root / "script"
        source = _safe_script_path(script_root, rel)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"unknown script file: {rel.as_posix()}")
        edited = _safe_script_path(script_root, saved_rel)
        if saved_rel != rel:
            source.unlink()
        edited.parent.mkdir(parents=True, exist_ok=True)
        edited.write_text(text)
        manifest = _safe_script_path(script_root, "stage.json")
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("snapshot stage.json is invalid")
        _retarget_manifest(manifest, rel, saved_rel)
        snapshot = capture_source_tree(
            source_root,
            root,
            parent_snapshot_id=base["snapshot_id"],
            label=f"Edited {saved_rel.as_posix()}",
            created_by_trigger_id=trigger_id,
        )
    return {"path": saved_rel.as_posix(), "snapshot": snapshot}


def _retarget_manifest(manifest_path, old_rel, new_rel):
    """Point stage.json's name/command at the newly saved script file."""
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("script/stage.json must contain a JSON object")
    command = manifest.get("command", "")
    for candidate in (old_rel.as_posix(), old_rel.name, str(manifest.get("name") or "")):
        if candidate and candidate in command:
            command = command.replace(candidate, new_rel.as_posix(), 1)
            break
    manifest["name"] = new_rel.as_posix()
    manifest["command"] = command
    write_json(manifest_path, manifest)


# ======================================================================
#  Workspace-level verbs
# ======================================================================

def workspace(root=None):
    root = resolve_root(root)
    return {"root": str(root), "project": project_entry(root)}


def restore(run_id, root=None):
    root = resolve_root(root)
    run, commit = restore_run_state(run_id, root)
    return {"run_id": run_id, "stage_commit": commit, "script_name": run.get("script_name")}


def diff_runs(run_a, run_b, root=None):
    root = resolve_root(root)
    a = get_run(run_a, root)
    b = get_run(run_b, root)
    if a.get("source_snapshot_id") and b.get("source_snapshot_id"):
        return diff_snapshots(a["source_snapshot_id"], b["source_snapshot_id"], root)
    return autoexp_git(
        ["diff", run_stage_commit(a), run_stage_commit(b), "--", *source_paths(root)],
        root=root, capture=True, check=False,
    )


def run_autoexp(run_id=None, root=None, *, snapshot_id=None, trigger_kind="mcp", actor_name="autoexp-mcp"):
    """Execute synchronously through the shared lifecycle."""
    from .execution import execute

    root = resolve_root(root)
    return execute(
        root=root,
        run_id=run_id,
        snapshot_id=snapshot_id,
        trigger_kind=trigger_kind,
        actor_name=actor_name,
    )


# ======================================================================
#  Doctor
# ======================================================================

def doctor(root=None):
    """Run a set of health checks and return {root, ok, checks}."""
    root = resolve_root(root)
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
        uses_ctx = "${CTX}" in manifest.get("command", "")
        detail = "" if uses_ctx else "command does not use ${CTX}; scripts can still use AUTOEXP_OUTPUT_DIR"
        add("stage_command_context", True, detail)
    except SystemExit as exc:
        add("stage_manifest_keys", False, str(exc))

    init_db(root)
    add("index.sqlite", (root / "index.sqlite").is_file())
    add("private_git", (root / ".autoexp" / "git").is_dir())

    conn = db(root)
    broken_runs = conn.execute(
        """select count(*) from runs r left join triggers t on t.trigger_id = r.trigger_id
           where r.status in ('success', 'failed', 'canceled')
             and coalesce(t.kind, '') != 'legacy'
             and (r.source_snapshot_id is null or r.trigger_id is null or
                  r.runner is null or r.runner_identity is null or
                  r.started_at is null or r.ended_at is null or r.duration_ms is null)"""
    ).fetchone()[0]
    add(
        "run_lifecycle_integrity",
        broken_runs == 0,
        "" if broken_runs == 0 else f"{broken_runs} terminal runs have incomplete lifecycle evidence",
    )
    if project_mode(root) == "autoresearch":
        broken_attempts = conn.execute(
            """select count(*) from research_attempts a
               left join runs r on r.run_id = a.run_id
               where a.run_id is not null and (
                   r.run_id is null or r.source_snapshot_id is not a.candidate_snapshot_id
               )"""
        ).fetchone()[0]
        incomplete_attempts = conn.execute(
            """select count(*) from research_attempts
               where status = 'scored' and (
                   base_snapshot_id is null or candidate_snapshot_id is null or run_id is null
               ) and metadata not like '%"legacy"%'"""
        ).fetchone()[0]
        add(
            "research_attempt_integrity",
            broken_attempts == 0 and incomplete_attempts == 0,
            "" if not (broken_attempts or incomplete_attempts)
            else f"{broken_attempts} run/snapshot mismatches; {incomplete_attempts} incomplete scored attempts",
        )
    conn.close()

    gitignore = root / ".gitignore"
    add("app.env_ignored", APP_ENV in gitignore.read_text() if gitignore.is_file() else False)

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

    if project_mode(root) == "autoresearch":
        from .autoresearch import for_project as research_for_project

        preflight = research_for_project(root).preflight()
        for item in preflight["checks"]:
            if item["name"] in {"project", "git", "private_git", "runs_directory"}:
                continue
            add(
                f"research_{item['name']}",
                item["ok"],
                item["detail"],
                required=item["required"],
            )

    overall_ok = all(item["ok"] or not item.get("required", True) for item in checks)
    return {"root": str(root), "ok": overall_ok, "checks": checks}
