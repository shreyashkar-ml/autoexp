"""One-time, non-destructive importer for repo-local Autoexp 0.2 projects."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path

from .store import db, private_git_dir
from .workspace import (
    PROJECT_CONFIG, PROJECT_REPORT_INSTRUCTIONS, create_experiment, declare_file,
    experiment_data_dir, now, read_json,
)


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def _tables(conn):
    return {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}



def _safe_import_file(data_root, run_dir, rel):
    base = (Path(data_root) / run_dir).resolve()
    data_root = Path(data_root).resolve()
    path = (base / rel).resolve()
    if Path(run_dir).is_absolute() or Path(rel).is_absolute() or not base.is_relative_to(data_root) or not path.is_relative_to(base):
        raise ValueError(f"unsafe imported artifact path: {run_dir}/{rel}")
    return path


def _validate_artifact_hashes(old, data_root):
    if "artifacts" not in _tables(old):
        return {"checked": 0, "ok": True}
    runs = {
        row["run_id"]: row["run_dir"]
        for row in old.execute("select run_id, run_dir from runs")
    }
    checked = 0
    for artifact in old.execute("select run_id, path, content_hash, size_bytes from artifacts"):
        path = _safe_import_file(data_root, runs[artifact["run_id"]], artifact["path"])
        if not path.is_file():
            raise ValueError(f"missing imported artifact: {artifact['path']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact["content_hash"] or path.stat().st_size != artifact["size_bytes"]:
            raise ValueError(f"imported artifact hash mismatch: {artifact['path']}")
        checked += 1
    return {"checked": checked, "ok": True}


def _validate_snapshot_hashes(old, data_root):
    if "source_snapshots" not in _tables(old):
        return {"checked": 0, "ok": True}
    from .git_store import materialize_commit
    from .snapshots import snapshot_hashes

    fields = ("script_hash", "params_hash", "manifest_hash", "runtime_config_hash", "source_hash")
    checked = 0
    for snapshot in old.execute("select * from source_snapshots"):
        with tempfile.TemporaryDirectory(prefix="autoexp-import-snapshot-") as tmp:
            materialize_commit(snapshot["git_commit"], tmp, data_root)
            candidates = (
                snapshot_hashes(tmp),
                snapshot_hashes(tmp, include_types=False),
            )
        if not any(all(actual[field] == snapshot[field] for field in fields) for actual in candidates):
            raise ValueError(f"imported snapshot hash mismatch: {snapshot['snapshot_id']}")
        checked += 1
    return {"checked": checked, "ok": True}

def _copy_rows(old, new, table, *, extras=None, renames=None):
    if table not in _tables(old):
        return 0
    extras = extras or {}
    renames = renames or {}
    old_columns = _columns(old, table)
    new_columns = _columns(new, table)
    rows = old.execute(f"select * from {table}").fetchall()
    for source in rows:
        value = dict(source)
        value.update(extras)
        value.update({target: value[source_name] for source_name, target in renames.items() if source_name in value})
        columns = [name for name in new_columns if name in value]
        new.execute(
            f"insert into {table}({', '.join(columns)}) values({', '.join('?' for _ in columns)})",
            [value[name] for name in columns],
        )
    return len(rows)


def _legacy_manifest(root, config):
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    editable = [f"experiment/{path}" for path in source.get("editable", [])]
    stage_path = root / ".autoexp/stage.json"
    stage = read_json(stage_path) if stage_path.is_file() else {}
    entry = stage.get("name")
    entry = f"experiment/{entry}" if entry and not str(entry).startswith("experiment/") else entry
    items = []
    for path in editable:
        items.append((path, "entrypoint" if path == entry else "editable-source"))
    if entry and entry not in {path for path, _ in items}:
        items.append((entry, "entrypoint"))
    for path in sorted((root / "experiment").rglob("*")) if (root / "experiment").is_dir() else []:
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel not in {name for name, _ in items}:
                items.append((rel, "supporting-source"))
    if (root / ".env").is_file():
        items.append((".env", "secret-source"))
    return items, stage


def _record_for_non_git_repo(root, config, stage, title, kind):
    path = str(root.resolve())
    repo_id = hashlib.sha256(path.encode()).hexdigest()[:16]
    experiment_id = f"exp_imported_{uuid.uuid4().hex[:8]}"
    data_path = experiment_data_dir(repo_id, experiment_id)
    timestamp = now()
    conn = db()
    conn.execute(
        "insert or ignore into repositories values (?, ?, ?, ?, ?)",
        (repo_id, root.name, path, timestamp, timestamp),
    )
    settings = {key: config.get(key) for key in ("sandbox", "runtime", "external_inputs", "autoresearch") if key in config}
    conn.execute(
        """insert into experiments values(
             ?, ?, ?, ?, ?, 'imported', ?, ?, ?, '{}', ?, ?, ?, ?, ?
           )""",
        (
            experiment_id, repo_id, title, config.get("description") or "Imported legacy experiment",
            kind, config.get("runner", "local"), json.dumps(stage), json.dumps(settings),
            json.dumps({"type": "object", "properties": {}}),
            (root / PROJECT_REPORT_INSTRUCTIONS).read_text() if (root / PROJECT_REPORT_INSTRUCTIONS).is_file() else "",
            str(data_path), timestamp, timestamp,
        ),
    )
    conn.commit()
    conn.close()
    for name in ("runs", "reports", "insights"):
        (data_path / name).mkdir(parents=True, exist_ok=True)
    return experiment_id


def import_legacy_project(path):
    source = Path(path).expanduser().resolve()
    old_db_path = source / ".autoexp/state.sqlite"
    config_path = source / PROJECT_CONFIG
    old_git = source / ".autoexp/repository"
    if not old_db_path.is_file() or not config_path.is_file() or not old_git.is_dir():
        raise ValueError("legacy project must contain .autoexp/state.sqlite, project.json, and repository")

    global_db = db()
    prior = global_db.execute("select summary from imports where source_path = ?", (str(source),)).fetchone()
    global_db.close()
    if prior:
        return json.loads(prior[0])

    config = read_json(config_path)
    manifest, stage = _legacy_manifest(source, config)
    kind = "autoresearch" if config.get("mode") == "autoresearch" or "autoresearch" in config else "standard"
    title = config.get("title") or source.name
    try:
        entry = create_experiment(
            config.get("description") or "Imported legacy experiment",
            root=source, title=title, kind=kind,
            entrypoint=next((path for path, role in manifest if role == "entrypoint"), None),
            command=stage.get("command"), working_dir=stage.get("working_dir"),
            runner=config.get("runner", "local"), config={key: config[key] for key in ("sandbox", "runtime", "external_inputs", "autoresearch") if key in config},
        )
        experiment_id = entry["experiment_id"]
    except ValueError as exc:
        if "Git worktree" not in str(exc):
            raise
        experiment_id = _record_for_non_git_repo(source, config, stage, title, kind)

    from .workspace import experiment_entry
    entry = experiment_entry(experiment_id)
    data_root = Path(entry["data_path"])
    for rel, role in manifest:
        declare_file(experiment_id, rel, role)

    destination_git = private_git_dir(data_root)
    if destination_git.is_dir():
        subprocess.run(
            ["git", "--git-dir", str(destination_git), "fetch", str(old_git), "+refs/*:refs/imported/*"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    else:
        destination_git.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old_git, destination_git)

    old_runs = source / "runs"
    if old_runs.is_dir():
        for run in old_runs.iterdir():
            target = data_root / "runs" / run.name
            if target.exists():
                raise ValueError(f"import destination already contains run: {run.name}")
            if run.is_dir():
                shutil.copytree(run, target)

    old_report = source / ".autoexp/project-report.md"
    if old_report.is_file():
        (data_root / "reports/report.md").write_bytes(old_report.read_bytes())

    old = sqlite3.connect(old_db_path)
    old.row_factory = sqlite3.Row
    new = db()
    summary = {"source": str(source), "experiment_id": experiment_id, "copied": {}, "validated": {}}
    try:
        summary["validated"]["artifact_hashes"] = _validate_artifact_hashes(old, data_root)
        summary["validated"]["snapshot_hashes"] = _validate_snapshot_hashes(old, data_root)
        new.execute("begin immediate")
        new.execute("pragma defer_foreign_keys = on")
        summary["copied"]["triggers"] = _copy_rows(old, new, "triggers", extras={"experiment_id": experiment_id})
        summary["copied"]["source_snapshots"] = _copy_rows(old, new, "source_snapshots", extras={"repo_id": entry["repo_id"], "experiment_id": experiment_id})
        summary["copied"]["runs"] = _copy_rows(old, new, "runs", extras={"experiment_id": experiment_id})
        for table in ("artifacts", "run_external_inputs"):
            summary["copied"][table] = _copy_rows(old, new, table)
        summary["copied"]["research_contracts"] = _copy_rows(old, new, "research_contracts", extras={"experiment_id": experiment_id})
        for table in ("research_sessions", "research_attempts"):
            summary["copied"][table] = _copy_rows(old, new, table)
        summary["copied"]["milestones"] = _copy_rows(old, new, "milestones", extras={"experiment_id": experiment_id})
        summary["copied"]["documents"] = _copy_rows(old, new, "documents", extras={"experiment_id": experiment_id})

        if old_report.is_file() and not new.execute(
            "select 1 from documents where experiment_id = ? and path = 'reports/report.md'",
            (experiment_id,),
        ).fetchone():
            report_path = data_root / "reports/report.md"
            new.execute(
                """insert into documents(
                     document_id, experiment_id, run_id, kind, title, path,
                     content_hash, size_bytes, created_at
                   ) values (?, ?, null, 'report', ?, 'reports/report.md', ?, ?, ?)""",
                (
                    f"document_{uuid.uuid4().hex}", experiment_id, title,
                    hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    report_path.stat().st_size, now(),
                ),
            )
            summary["copied"]["documents"] += 1

        for table in ("runs", "source_snapshots", "artifacts", "documents"):
            expected = summary["copied"].get(table, 0)
            foreign = (
                "run_id in (select run_id from runs where experiment_id = ?)"
                if table == "artifacts"
                else "experiment_id = ?"
            )
            actual = new.execute(
                f"select count(*) from {table} where {foreign}", (experiment_id,)
            ).fetchone()[0]
            summary["validated"][table] = {
                "expected": expected, "actual": actual, "ok": expected == actual,
            }
            if expected != actual:
                raise ValueError(f"import validation failed for {table}: {actual}/{expected}")
        summary["source_preserved"] = True
        summary["removable_after_validation"] = [
            str(source / ".autoexp"), str(source / "runs"),
        ]
        import_id = f"import_{uuid.uuid4().hex}"
        new.execute(
            "insert into imports values (?, ?, ?, ?, ?)",
            (import_id, str(source), experiment_id, json.dumps(summary, sort_keys=True), now()),
        )
        new.commit()
    except Exception:
        new.rollback()
        raise
    finally:
        old.close()
        new.close()
    return summary
