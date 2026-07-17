"""Global report guidance, immutable documents, and synthesis evidence."""

import hashlib
import uuid
from pathlib import Path

from .runs import get_run, script_name, source_root_for_run
from .workspace import (
    PARAMS_FILE, PROJECT_REPORT, experiment_entry, experiment_id, manifest_files,
    now, repository_root, resolve_root, run_dir_for, safe_repository_path, write_json,
)


REPORT_CONTRACT = """Use the global run report bundle as the source of truth. Read only the referenced immutable outputs, logs, source snapshot, report guidance, and safe secret-key availability metadata. Never request or include secret values. Write generated report files under the run's global report directory."""
PROJECT_REPORT_CONTRACT = """Synthesize the experiment as a whole from recorded runs, milestones, reports, insights, and Autoresearch attempts. Distinguish evidence from inference and cite run IDs."""


def _hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _redacted_bytes(data, root):
    from .runner import app_env

    for value in sorted({value.encode() for value in app_env(root).values() if value}, key=len, reverse=True):
        data = data.replace(value, b"[redacted]")
    return data


def app_env_keys(root=None):
    return [
        key["name"]
        for item in manifest_files(root)
        if item["role"] == "secret-source"
        for key in item["secret_keys"]
    ]


def report_instruction(root=None):
    entry = experiment_entry(resolve_root(root))
    return {"source": "global:report-guidance", "text": entry["report_guidance"]}


def report_generation_instruction(root=None):
    instruction = report_instruction(root)
    return {**instruction, "text": f"{instruction['text'].rstrip()}\n\n{REPORT_CONTRACT}\n"}


def set_report_instruction(path, root=None):
    root = resolve_root(root)
    source = Path(path).expanduser()
    if not source.is_absolute():
        source = safe_repository_path(root, source)
    if not source.is_file() or not source.resolve().is_relative_to(repository_root(root)):
        raise ValueError("report guidance must be a file inside the registered repository")
    return write_report_instruction(source.read_text(), root)


def write_report_instruction(text, root=None):
    from .store import db

    if not isinstance(text, str):
        raise ValueError("text must be a string")
    root = resolve_root(root)
    from .runner import redact_secrets
    text = redact_secrets(text, root)
    conn = db()
    conn.execute(
        "update experiments set report_guidance = ?, updated_at = ? where experiment_id = ?",
        (text, now(), experiment_id(root)),
    )
    conn.commit()
    conn.close()
    return report_instruction(root)


def _document(root, path, kind, title, run_id=None):
    from .store import db

    root = resolve_root(root)
    path = Path(path).resolve()
    if kind not in {"report", "insight"}:
        raise ValueError("document kind must be report or insight")
    if not path.is_file() or not path.is_relative_to(root.resolve()):
        raise ValueError("document must stay inside global Autoexp storage")
    rel = path.relative_to(root).as_posix()
    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    conn = db()
    if run_id:
        owner = conn.execute("select experiment_id from runs where run_id = ?", (run_id,)).fetchone()
        if not owner or owner["experiment_id"] != experiment_id(root):
            conn.close()
            raise ValueError("document run_id must belong to this experiment")
    conn.execute(
        """insert into documents(document_id, experiment_id, run_id, kind, title, path,
             content_hash, size_bytes, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            document_id, experiment_id(root), run_id, kind, title, rel,
            _hash_file(path), path.stat().st_size, now(),
        ),
    )
    conn.commit()
    row = conn.execute("select * from documents where document_id = ?", (document_id,)).fetchone()
    conn.close()
    return dict(row)


def list_documents(root=None, kind=None):
    from .store import db

    root = resolve_root(root)
    conn = db()
    sql = "select * from documents where experiment_id = ?"
    args = [experiment_id(root)]
    if kind:
        sql += " and kind = ?"
        args.append(kind)
    rows = conn.execute(sql + " order by created_at desc, rowid desc", args).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def add_document(path, *, kind, title=None, root=None, run_id=None):
    root = resolve_root(root)
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    directory = root / ("insights" if kind == "insight" else "reports")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{source.stem}-{uuid.uuid4().hex[:8]}{source.suffix}"
    destination.write_bytes(_redacted_bytes(source.read_bytes(), root))
    return _document(root, destination, kind, title or source.stem.replace("_", " "), run_id)


def read_project_report(root=None):
    root = resolve_root(root)
    documents = [item for item in list_documents(root, "report") if item.get("run_id") is None]
    if documents:
        path = root / documents[0]["path"]
    else:
        path = root / PROJECT_REPORT
    return {
        "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else PROJECT_REPORT,
        "text": path.read_text(errors="replace") if path.is_file() else "",
        "exists": path.is_file(),
    }


def write_project_report(text, root=None):
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    root = resolve_root(root)
    from .runner import redact_secrets
    path = root / "reports" / f"experiment-report-{uuid.uuid4().hex[:8]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_secrets(text, root))
    return _document(root, path, "report", "Experiment report")


def mark_milestone(*, title, significance, run_id=None, attempt_id=None, actor_name=None, root=None):
    from .store import db, init_db

    root = resolve_root(root)
    init_db(root)
    if bool(run_id) == bool(attempt_id):
        raise ValueError("provide exactly one of run_id or attempt_id")
    if not isinstance(title, str) or not title.strip() or not isinstance(significance, str) or not significance.strip():
        raise ValueError("title and significance are required")
    kind, target = ("run", run_id) if run_id else ("attempt", attempt_id)
    conn = db()
    if kind == "run":
        exists = conn.execute(
            "select 1 from runs where run_id = ? and experiment_id = ?",
            (target, experiment_id(root)),
        ).fetchone()
    else:
        exists = conn.execute(
            """select 1 from research_attempts a join research_contracts c
                 on c.contract_id = a.contract_id
               where a.attempt_id = ? and c.experiment_id = ?""",
            (target, experiment_id(root)),
        ).fetchone()
    if not exists:
        conn.close()
        raise ValueError(f"unknown {kind}: {target}")
    from .runner import redact_secrets
    milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "insert into milestones values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            milestone_id, experiment_id(root), kind, target,
            redact_secrets(title.strip()[:120], root),
            redact_secrets(significance.strip(), root),
            redact_secrets(str(actor_name), root) if actor_name is not None else None,
            now(),
        ),
    )
    conn.commit()
    conn.close()
    return next(item for item in list_milestones(root) if item["milestone_id"] == milestone_id)


def list_milestones(root=None):
    from .store import db, init_db

    root = resolve_root(root)
    init_db(root)
    conn = db()
    rows = conn.execute(
        "select * from milestones where experiment_id = ? order by created_at desc, rowid desc",
        (experiment_id(root),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def project_summary(root=None, limit=20):
    from .runtime import list_runs
    from .workspace import project_mode

    root = resolve_root(root)
    result = {
        "experiment": experiment_entry(root), "mode": project_mode(root),
        "project_report": read_project_report(root), "documents": list_documents(root),
        "milestones": list_milestones(root), "runs": list_runs(limit, root),
        "report_contract": PROJECT_REPORT_CONTRACT,
    }
    if result["mode"] == "autoresearch":
        from .autoresearch import for_project
        result["autoresearch"] = for_project(root).state()
    return result


def write_report_bundle(run_id, root=None):
    root = resolve_root(root)
    run = get_run(run_id, root)
    run_dir = run_dir_for(run, root)
    if not run_dir.exists():
        raise FileNotFoundError(f"missing run directory: {run_dir}")
    source_root = source_root_for_run(run, root)
    report_dir = run_dir / "report"
    bundle_path = report_dir / "report_bundle.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    from .artifacts import list_artifacts
    indexed = list_artifacts(run_id, root)
    bundle = {
        "bundle_path": bundle_path.relative_to(root).as_posix(), "run_id": run_id,
        "script": run.get("script_name") or script_name(run_id, source_root),
        "report_dir": report_dir.relative_to(root).as_posix(),
        "secret_keys": app_env_keys(root), "instruction": "global:report-guidance",
        "script_params": (source_root / PARAMS_FILE).relative_to(root).as_posix(),
        "run": {key: run.get(key) for key in ("status", "created_at", "output_hash", "capsule_hash")},
        "artifacts": {
            category: [f"{run['run_dir']}/{item['path']}" for item in indexed if item["category"] == category]
            for category in ("output", "log", "report")
        },
    }
    write_json(bundle_path, bundle)
    return bundle
