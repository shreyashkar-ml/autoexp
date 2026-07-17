"""Trigger and secret-safe external-input provenance."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from .runner import SECRET_KEY, app_env, redaction_env_values
from .runs import get_run
from .snapshots import get_snapshot
from .store import db, init_db
from .workspace import PROJECT_CONFIG, experiment_id, now, read_json, repository_root, resolve_root


TRIGGER_KINDS = {"human", "ui", "cli", "agent", "autoresearch", "legacy"}
INPUT_KINDS = {"env", "secret", "file", "mount", "network", "external-service", "service"}


def _safe_metadata(value, key="", secrets=()):
    if SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _safe_metadata(v, str(k), secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted({str(item) for item in secrets if item}, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _json(value, secrets=()):
    return json.dumps(_safe_metadata(value or {}, secrets=secrets), sort_keys=True, separators=(",", ":"))


def create_trigger(
    kind,
    root=None,
    *,
    actor_name=None,
    session_id=None,
    request_id=None,
    metadata=None,
):
    root = resolve_root(root)
    init_db(root)
    if kind not in TRIGGER_KINDS:
        raise ValueError(f"trigger kind must be one of: {', '.join(sorted(TRIGGER_KINDS))}")
    secrets = redaction_env_values(root)
    trigger = {
        "trigger_id": f"trigger_{uuid.uuid4().hex}",
        "kind": kind,
        "experiment_id": experiment_id(root),
        "actor_name": _safe_metadata(actor_name, secrets=secrets),
        "session_id": _safe_metadata(session_id, secrets=secrets),
        "request_id": _safe_metadata(request_id, secrets=secrets),
        "metadata": _json(metadata, secrets),
        "created_at": now(),
    }
    conn = db(root)
    conn.execute(
        """insert into triggers(
               trigger_id, experiment_id, kind, actor_name, session_id, request_id, metadata, created_at
           ) values(
               :trigger_id, :experiment_id, :kind, :actor_name, :session_id, :request_id, :metadata, :created_at
           )""",
        trigger,
    )
    conn.commit()
    conn.close()
    return {**trigger, "metadata": json.loads(trigger["metadata"])}


def get_trigger(trigger_id, root=None):
    if not trigger_id:
        return None
    root = resolve_root(root)
    conn = db(root)
    row = conn.execute(
        "select * from triggers where trigger_id = ?", (trigger_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    trigger = dict(row)
    try:
        trigger["metadata"] = json.loads(trigger.get("metadata") or "{}")
    except json.JSONDecodeError:
        trigger["metadata"] = {}
    return trigger


def _declarations(config):
    raw = config.get("external_inputs", [])
    if isinstance(raw, dict):
        raw = [({"name": name, **spec} if isinstance(spec, dict) else {"name": name}) for name, spec in raw.items()]
    return [item for item in raw if isinstance(item, dict) and isinstance(item.get("name"), str)]


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_record(spec, environment, root):
    name = spec["name"]
    kind = spec.get("kind", "env")
    if kind not in INPUT_KINDS:
        raise ValueError(f"invalid external input kind for {name}: {kind}")
    if kind == "service":
        kind = "external-service"

    version = spec.get("version")
    fingerprint = None
    metadata = {key: spec[key] for key in ("description", "required") if key in spec}
    if kind in {"env", "secret"}:
        present = name in environment
        if kind != "secret" and present and spec.get("fingerprint") is True:
            fingerprint = hashlib.sha256(environment[name].encode()).hexdigest()
    elif kind in {"file", "mount"}:
        raw_path = spec.get("path") or name
        path = Path(raw_path).expanduser()
        path = path if path.is_absolute() else repository_root(root) / path
        present = path.exists()
        metadata["path"] = raw_path
        if present and path.is_file() and spec.get("fingerprint", True):
            fingerprint = _file_hash(path)
    else:
        present = bool(spec.get("present", True))

    state = "pinned" if version or fingerprint else "unpinned"
    if kind == "secret" and spec.get("redacted") is True:
        state = "redacted"
    return {
        "name": name,
        "kind": kind,
        "present": int(present),
        "fingerprint": fingerprint,
        "version": str(version) if version is not None else None,
        "reproducibility_state": state,
        "metadata": _json(metadata, redaction_env_values(root)),
    }


def inventory_external_inputs(source_root, root=None, environment_overrides=None):
    """Build a secret-safe input inventory before run allocation."""
    root = resolve_root(root)
    source_root = Path(source_root)
    config = read_json(source_root / PROJECT_CONFIG)
    if not isinstance(config, dict):
        raise ValueError(f"{PROJECT_CONFIG} must contain a JSON object")
    env_file = app_env(root)
    overrides = {str(key): str(value) for key, value in (environment_overrides or {}).items()}
    environment = env_file | overrides if config.get("runner") == "docker" else os.environ | env_file | overrides
    records = {
        record["name"]: record
        for record in (_input_record(spec, environment, root) for spec in _declarations(config))
    }
    for name in env_file:
        records.setdefault(name, {
            "name": name,
            "kind": "secret",
            "present": 1,
            "fingerprint": None,
            "version": None,
            "reproducibility_state": "unpinned",
            "metadata": "{}",
        })
    for name, value in overrides.items():
        secret = bool(SECRET_KEY.search(name))
        records.setdefault(name, {
            "name": name,
            "kind": "secret" if secret else "env",
            "present": 1,
            "fingerprint": None if secret else hashlib.sha256(value.encode()).hexdigest(),
            "version": None,
            "reproducibility_state": "redacted" if secret else "pinned",
            "metadata": "{}",
        })
    return [records[name] for name in sorted(records)]


def external_input_identity(records):
    """The safe input fields that participate in capsule identity."""
    keys = (
        "name", "kind", "present", "fingerprint", "version",
        "reproducibility_state",
    )
    return [{key: record.get(key) for key in keys} for record in records]


def record_external_inputs(run_id, records, root=None):
    root = resolve_root(root)

    conn = db(root)
    for record in records:
        conn.execute(
            """insert into run_external_inputs(
                   run_id, name, kind, present, fingerprint, version,
                   reproducibility_state, metadata
               ) values(
                   :run_id, :name, :kind, :present, :fingerprint, :version,
                   :reproducibility_state, :metadata
               )
               on conflict(run_id, name) do update set
                   kind = excluded.kind,
                   present = excluded.present,
                   fingerprint = excluded.fingerprint,
                   version = excluded.version,
                   reproducibility_state = excluded.reproducibility_state,
                   metadata = excluded.metadata""",
            {"run_id": run_id, **record},
        )
    conn.commit()
    conn.close()
    return external_inputs(run_id, root)


def capture_external_inputs(run_id, source_root, root=None):
    """Record declared inputs and .env key presence, never their values."""
    root = resolve_root(root)
    return record_external_inputs(
        run_id,
        inventory_external_inputs(source_root, root),
        root,
    )


def external_inputs(run_id, root=None):
    root = resolve_root(root)
    conn = db(root)
    rows = conn.execute(
        "select * from run_external_inputs where run_id = ? order by name", (run_id,)
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["present"] = bool(item["present"])
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def reproducibility_summary(run_id, root=None):
    root = resolve_root(root)
    run = get_run(run_id, root)
    snapshot = get_snapshot(run["source_snapshot_id"], root) if run.get("source_snapshot_id") else None
    inputs = external_inputs(run_id, root)
    unpinned = [item for item in inputs if item["reproducibility_state"] != "pinned"]
    runner_identity = run.get("runner_identity") or ""
    runtime_pinned = run.get("runner") == "docker" and "@sha256:" in runner_identity
    checks = {
        "source": {
            "state": "verified" if snapshot else "unknown",
            "hash": snapshot.get("script_hash") if snapshot else None,
        },
        "params": {
            "state": "verified" if snapshot else "unknown",
            "hash": snapshot.get("params_hash") if snapshot else None,
        },
        "runtime": {
            "state": "verified" if runtime_pinned else "unpinned",
            "identity": runner_identity or None,
        },
        "external_inputs": {
            "state": "verified" if not unpinned else "warning",
            "count": len(inputs),
            "unpinned": len(unpinned),
        },
    }
    return {
        "state": "verified" if all(item["state"] == "verified" for item in checks.values()) else "warning",
        "checks": checks,
        "external_inputs": inputs,
    }


def reproduction_state(run_id, root=None):
    root = resolve_root(root)
    run = get_run(run_id, root)
    if run.get("reproduces_run_id"):
        return {"state": "reproduction", "run_id": run["reproduces_run_id"]}
    if run.get("status") != "success" or not run.get("output_hash"):
        return {"state": "none", "run_id": None}
    conn = db(root)
    row = conn.execute(
        """select run_id from runs
           where experiment_id = ? and run_id != ? and status = 'success' and capsule_hash = ?
             and output_hash != ?
           order by created_at desc, rowid desc limit 1""",
        (experiment_id(root), run_id, run["capsule_hash"], run["output_hash"]),
    ).fetchone()
    conn.close()
    return {"state": "divergence" if row else "none", "run_id": row[0] if row else None}
