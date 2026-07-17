import json
import tempfile
from collections import Counter
from pathlib import Path

from .artifacts import (
    artifact_content,
    list_artifacts,
    read_log,
)
from .provenance import (
    get_trigger,
    reproducibility_summary,
    reproduction_state,
)
from .reports import write_report_bundle
from .runs import get_run, restore_run_state
from .snapshots import (
    diff_snapshots,
    get_snapshot,
    materialize_snapshot,
)
from .store import (
    autoexp_git,
    db,
)
from .workspace import (
    PARAMS_FILE, PROJECT_CONFIG, STAGE_MANIFEST,
    experiment_id,
    read_json,
    resolve_root,
    source_paths,
)


# Shared read/run verbs for the CLI and read-only HTTP evidence server.


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
    """The primary indexed report path, using its indexed global path."""
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
    trigger = get_trigger(run["trigger_id"], root) if run.get("trigger_id") else None
    metadata = (trigger or {}).get("metadata") or {}
    run["title"] = metadata.get("title") or _report_title(run, root)
    return run


def _report_title(run, root):
    """Best-effort short title for older runs that predate explicit run titles."""
    path = run.get("report_path")
    if path:
        target = Path(root) / path
        if target.is_file() and target.resolve().is_relative_to(Path(root).resolve()):
            for line in target.read_text(errors="replace").splitlines()[:20]:
                heading = line.removeprefix("+").strip()
                if heading.startswith("# "):
                    title = heading[2:].strip()
                    return title.removesuffix(" report")[:80]
    name = Path(run.get("script_name") or "run").stem.replace("_", " ").replace("-", " ")
    return name.capitalize()


def list_runs(limit=20, root=None):
    root = resolve_root(root)
    limit = max(1, min(int(limit), 200))
    conn = db(root)
    rows = conn.execute(
        """select r.*, s.params_hash, s.manifest_hash,
                  s.runtime_config_hash as snapshot_runtime_hash
           from runs r left join source_snapshots s on s.snapshot_id = r.source_snapshot_id
           where r.experiment_id = ? order by r.created_at desc, r.rowid desc limit ?""",
        (experiment_id(root), limit),
    ).fetchall()
    conn.close()
    runs = [run_row(row, root) for row in rows]
    for index, run in enumerate(runs):
        previous = runs[index + 1] if index + 1 < len(runs) else None
        changes = []
        if previous:
            if run.get("script_hash") != previous.get("script_hash"):
                changes.append("code")
            if run.get("params_hash") != previous.get("params_hash"):
                changes.append("params")
            if run.get("manifest_hash") != previous.get("manifest_hash"):
                changes.append("stage")
            if run.get("snapshot_runtime_hash") != previous.get("snapshot_runtime_hash"):
                changes.append("runtime")
            if not changes and run.get("capsule_hash") == previous.get("capsule_hash") and run.get("output_hash") != previous.get("output_hash"):
                changes.append("output drift")
        run["changes"] = changes or (["baseline"] if previous is None else ["same inputs"])
    return runs


def _safe_project_path(project_root, rel):
    project_root = Path(project_root)
    resolved_root = project_root.resolve()
    path = project_root / rel
    cursor = project_root
    for part in Path(rel).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"project path must not contain symlinks: {rel}")
    if not path.resolve(strict=False).is_relative_to(resolved_root):
        raise ValueError(f"path must stay inside the project: {rel}")
    return path

def _editable_source_files(source_root):
    config = read_json(_safe_project_path(source_root, PROJECT_CONFIG))
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    return tuple(source.get("editable") or ())

def run_source(run_id, root=None):
    """List a run's source files (excluding manifest/params), with one selected."""
    root = resolve_root(root)
    run = get_run(run_id, root)
    def read_files(source_root):
        files = []
        for rel in sorted(_editable_source_files(source_root)):
            item = _safe_project_path(source_root, rel)
            if item.is_symlink() or not item.is_file():
                continue
            files.append({"path": rel, "text": item.read_text(errors="replace")})
        return files

    if not run.get("source_snapshot_id"):
        raise ValueError(f"run {run_id} has no immutable source snapshot")
    with tempfile.TemporaryDirectory(prefix="autoexp-run-source-") as tmp:
        source_root = Path(tmp)
        materialize_snapshot(run["source_snapshot_id"], source_root, root)
        files = read_files(source_root)
        managed = {}
        for rel in (PARAMS_FILE, STAGE_MANIFEST):
            path = _safe_project_path(source_root, rel)
            if path.is_file():
                managed[rel] = path.read_text(errors="replace")

    wanted = run.get("script_name") or ""
    selected = next((f["path"] for f in files if f["path"] == wanted), "")
    if not selected and files:
        selected = files[0]["path"]
    return {"run_id": run_id, "script": run.get("script_name"), "selected": selected, "files": files, "managed": managed}


def run_report(run_id, root=None):
    root = resolve_root(root)
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
#  Workspace-level verbs
# ======================================================================




def diff_runs(run_a, run_b, root=None):
    root = resolve_root(root)
    a = get_run(run_a, root)
    b = get_run(run_b, root)
    if not a.get("source_snapshot_id") or not b.get("source_snapshot_id"):
        raise ValueError("both runs must have immutable source snapshots")
    return diff_snapshots(a["source_snapshot_id"], b["source_snapshot_id"], root)



# ======================================================================
#  Doctor
# ======================================================================

def doctor(root=None):
    """Validate one registered experiment and its global evidence plane."""
    from .execution import preflight_request
    from .store import private_git_dir
    from .workspace import repository_root, user_data_dir

    root = resolve_root(root)
    checks = []
    def add(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "required": bool(required)})

    add("repository", repository_root(root).is_dir(), repository_root(root))
    add("global_data", root.is_dir() and root.is_relative_to(user_data_dir()), root)
    add("state.sqlite", (user_data_dir() / "state.sqlite").is_file())
    add("private_git", private_git_dir(root).is_dir(), private_git_dir(root))
    try:
        result = preflight_request(root)
        checks.extend({**item, "name": f"preflight_{item['name']}"} for item in result["checks"])
    except Exception as exc:
        add("preflight", False, exc)

    conn = db()
    broken = conn.execute("""select count(*) from runs where experiment_id = ? and status in ('success', 'failed', 'canceled') and (source_snapshot_id is null or trigger_id is null or runner is null or ended_at is null)""", (experiment_id(root),)).fetchone()[0]
    conn.close()
    add("run_lifecycle_integrity", broken == 0, f"{broken} terminal runs have incomplete evidence" if broken else "")
    overall = all(item["ok"] or not item.get("required", True) for item in checks)
    return {"root": str(root), "ok": overall, "checks": checks}
