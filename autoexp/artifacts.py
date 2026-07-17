"""Indexed, run-scoped artifact reads."""

import base64
import codecs
import csv
import hashlib
import io
import json
import mimetypes
import struct
import uuid
from pathlib import Path

from .runs import get_run
from .store import db, init_db
from .workspace import now, resolve_root, run_dir_for


MAX_PREVIEW_BYTES = 256 * 1024
MAX_CONTENT_BYTES = 16 * 1024 * 1024
TERMINAL_STATUSES = {"success", "failed", "canceled"}


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(path):
    if path.suffix.lower() == ".log":
        return "text/plain"
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _image_size(path, media_type):
    with path.open("rb") as handle:
        header = handle.read(24)
    if media_type == "image/png" and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", header[16:24])
    if media_type == "image/gif" and header[:6] in {b"GIF87a", b"GIF89a"}:
        return struct.unpack("<HH", header[6:10])
    return None


def _metadata(path, media_type):
    size = path.stat().st_size
    metadata = {}
    if media_type == "application/json" and size <= MAX_PREVIEW_BYTES:
        try:
            value = json.loads(path.read_text())
            metadata.update(valid_json=True, root_type=type(value).__name__)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata["valid_json"] = False
    elif media_type == "text/csv":
        with path.open("rb") as handle:
            raw = handle.read(min(size, 64 * 1024))
        text = raw.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))[:6]
        metadata.update(
            columns=rows[0] if rows else [],
            preview_rows=max(0, len(rows) - 1),
            preview_truncated=size > len(raw),
        )
    elif media_type.startswith("image/"):
        dimensions = _image_size(path, media_type)
        if dimensions:
            metadata.update(width=dimensions[0], height=dimensions[1])
    elif media_type.startswith("text/"):
        with path.open("rb") as handle:
            sample = handle.read(min(size, 4096))
        try:
            sample.decode("utf-8")
            metadata["readable"] = True
        except UnicodeDecodeError:
            metadata["readable"] = False
    return metadata


def _decode(row):
    artifact = dict(row)
    try:
        artifact["metadata"] = json.loads(artifact.get("metadata") or "{}")
    except json.JSONDecodeError:
        artifact["metadata"] = {}
    artifact["content_url"] = (
        f"/api/runs/{artifact['run_id']}/artifacts/{artifact['artifact_id']}/content"
    )
    return artifact


def _safe_path(run, artifact, root):
    run_root = run_dir_for(run, root).resolve()
    rel = Path(artifact["path"])
    if rel.is_absolute() or ".." in rel.parts or not rel.name:
        raise ValueError("artifact path must stay inside its run")
    path = (run_root / rel).resolve()
    if not path.is_relative_to(run_root) or not path.is_file():
        raise FileNotFoundError(f"missing indexed artifact: {artifact['path']}")
    if (artifact["category"] == "report" or run["status"] in TERMINAL_STATUSES) and (
        path.stat().st_size != artifact["size_bytes"]
        or _hash_file(path) != artifact["content_hash"]
    ):
        raise ValueError(f"indexed artifact content changed: {artifact['path']}")
    return path


def _upsert(run, path, category, root):
    run_root = run_dir_for(run, root).resolve()
    path = Path(path)
    resolved = path.resolve()
    if not resolved.is_relative_to(run_root) or not resolved.is_file():
        raise ValueError("artifact path must stay inside its run")
    rel = path.absolute().relative_to(run_root).as_posix()
    media_type = _media_type(resolved)
    content_hash = _hash_file(resolved)
    size = resolved.stat().st_size
    metadata = json.dumps(_metadata(resolved, media_type), sort_keys=True)

    conn = db(root)
    existing = conn.execute(
        "select * from artifacts where run_id = ? and path = ?",
        (run["run_id"], rel),
    ).fetchone()
    if existing:
        existing = dict(existing)
        changed = (
            existing["content_hash"] != content_hash
            or existing["size_bytes"] != size
            or existing["media_type"] != media_type
        )
        if changed and (category == "report" or run["status"] in TERMINAL_STATUSES):
            conn.close()
            raise ValueError(f"artifact is immutable; attach a new path instead: {rel}")
        if changed:
            conn.execute(
                """update artifacts
                   set media_type = ?, content_hash = ?, size_bytes = ?, metadata = ?
                   where artifact_id = ?""",
                (media_type, content_hash, size, metadata, existing["artifact_id"]),
            )
    else:
        conn.execute(
            """insert into artifacts(
                   artifact_id, run_id, category, path, media_type, content_hash,
                   size_bytes, created_at, metadata
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"artifact_{uuid.uuid4().hex}",
                run["run_id"],
                category,
                rel,
                media_type,
                content_hash,
                size,
                now(),
                metadata,
            ),
        )
    conn.commit()
    row = conn.execute(
        "select * from artifacts where run_id = ? and path = ?",
        (run["run_id"], rel),
    ).fetchone()
    conn.close()
    return _decode(row)


def _index_directory(run, directory, category, root, *, exclude=(), strict=True):
    base = run_dir_for(run, root) / directory
    if not base.is_dir():
        return []
    artifacts = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.name not in exclude:
            try:
                artifacts.append(_upsert(run, path, category, root))
            except (FileNotFoundError, ValueError):
                if strict:
                    raise
    return artifacts


def index_execution_artifacts(run_id, root=None):
    """Index output and process streams while the run is still mutable."""
    root = resolve_root(root)
    init_db(root)
    run = get_run(run_id, root)
    from .runner import scrub_secrets
    scrub_secrets(run_dir_for(run, root), root)
    return [
        *_index_directory(run, "logs", "log", root),
        *_index_directory(run, "output", "output", root),
    ]


def index_report_artifacts(run_id, root=None):
    """Attach new reports; an indexed report path can never be replaced."""
    root = resolve_root(root)
    init_db(root)
    run = get_run(run_id, root)
    from .runner import scrub_secrets
    scrub_secrets(run_dir_for(run, root), root)
    return _index_directory(
        run,
        "report",
        "report",
        root,
        exclude={"report_bundle.json"},
    )


def list_artifacts(run_id, root=None, category=None):
    root = resolve_root(root)
    init_db(root)
    get_run(run_id, root)
    conn = db(root)
    if category:
        rows = conn.execute(
            """select * from artifacts where run_id = ? and category = ?
               order by path, artifact_id""",
            (run_id, category),
        ).fetchall()
    else:
        rows = conn.execute(
            "select * from artifacts where run_id = ? order by category, path, artifact_id",
            (run_id,),
        ).fetchall()
    conn.close()
    return [_decode(row) for row in rows]


def _artifact(run_id, artifact_id, root):
    run = get_run(run_id, root)
    conn = db(root)
    row = conn.execute(
        "select * from artifacts where run_id = ? and artifact_id = ?",
        (run_id, artifact_id),
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"unknown artifact_id for run {run_id}: {artifact_id}")
    return run, _decode(row)


def artifact_content(run_id, artifact_id, root=None, *, offset=0, limit=MAX_CONTENT_BYTES):
    root = resolve_root(root)
    run, artifact = _artifact(run_id, artifact_id, root)
    path = _safe_path(run, artifact, root)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_CONTENT_BYTES))
    with path.open("rb") as handle:
        handle.seek(offset)
        content = handle.read(limit)
    return artifact, content


def artifact_detail(run_id, artifact_id, root=None):
    root = resolve_root(root)
    run, artifact = _artifact(run_id, artifact_id, root)
    path = _safe_path(run, artifact, root)
    media_type = artifact["media_type"]
    size = artifact["size_bytes"]
    with path.open("rb") as handle:
        raw = handle.read(min(size, MAX_PREVIEW_BYTES))
    truncated = size > len(raw)

    if media_type == "application/json":
        try:
            preview = {"kind": "json", "value": json.loads(raw), "truncated": False}
        except (UnicodeDecodeError, json.JSONDecodeError):
            preview = {"kind": "text", "text": raw.decode(errors="replace"), "truncated": truncated}
    elif media_type == "text/csv":
        rows = list(csv.reader(io.StringIO(raw.decode(errors="replace"))))
        preview = {
            "kind": "csv",
            "columns": rows[0] if rows else [],
            "rows": rows[1:51],
            "truncated": truncated or len(rows) > 51,
        }
    elif media_type.startswith("text/"):
        preview = {"kind": "text", "text": raw.decode(errors="replace"), "truncated": truncated}
    elif media_type.startswith("image/"):
        preview = {"kind": "image", "content_url": artifact["content_url"]}
    else:
        preview = {
            "kind": "binary",
            "base64": base64.b64encode(raw[:4096]).decode(),
            "truncated": size > 4096,
        }
    return {**artifact, "preview": preview}


def read_log(run_id, stream, offset=0, limit=65536, root=None):
    if stream not in {"stdout", "stderr"}:
        raise ValueError("stream must be stdout or stderr")
    root = resolve_root(root)
    run = get_run(run_id, root)
    rel = f"logs/script.{stream}.log"
    conn = db(root)
    row = conn.execute(
        "select * from artifacts where run_id = ? and path = ?",
        (run_id, rel),
    ).fetchone()
    conn.close()
    path = run_dir_for(run, root) / rel
    if not row and run["status"] not in TERMINAL_STATUSES and path.is_file():
        artifact = _upsert(run, path, "log", root)
    elif row:
        artifact = _decode(row)
    else:
        return {
            "run_id": run_id,
            "stream": stream,
            "text": "",
            "next_offset": max(0, int(offset)),
            "terminal": run["status"] in TERMINAL_STATUSES,
        }

    safe = _safe_path(run, artifact, root)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_CONTENT_BYTES))
    with safe.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(limit)
    terminal = (
        run["status"] in TERMINAL_STATUSES
        and offset + len(raw) >= safe.stat().st_size
    )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(raw, final=terminal)
    pending = b"" if terminal else decoder.getstate()[0]
    return {
        "run_id": run_id,
        "stream": stream,
        "text": text,
        "next_offset": offset + len(raw) - len(pending),
        "terminal": terminal,
    }
