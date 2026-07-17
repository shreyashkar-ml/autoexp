"""Short-lived, single-completion browser review sessions."""

import hashlib
import json
import secrets
import time
import uuid

from .store import db
from .workspace import experiment_id, now, resolve_root


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_review_session(root=None, *, ttl=900):
    root = resolve_root(root)
    ttl = max(60, min(int(ttl), 3600))
    token = secrets.token_urlsafe(32)
    session = {
        "session_id": f"review_{uuid.uuid4().hex}", "experiment_id": experiment_id(root),
        "token_hash": _hash(token), "status": "waiting",
        "expires_at": int(time.time()) + ttl, "notes": "[]",
        "created_at": now(), "completed_at": None,
    }
    conn = db()
    conn.execute(
        """insert into review_sessions(session_id, experiment_id, token_hash, status,
             expires_at, notes, created_at, completed_at)
           values(:session_id, :experiment_id, :token_hash, :status,
             :expires_at, :notes, :created_at, :completed_at)""",
        session,
    )
    conn.commit()
    conn.close()
    return token, {**session, "notes": []}


def review_session(token):
    if not isinstance(token, str) or len(token) < 32:
        return None
    conn = db()
    row = conn.execute("select * from review_sessions where token_hash = ?", (_hash(token),)).fetchone()
    if row and row["status"] == "waiting" and row["expires_at"] <= int(time.time()):
        conn.execute("update review_sessions set status = 'expired' where session_id = ? and status = 'waiting'", (row["session_id"],))
        conn.commit()
        row = conn.execute("select * from review_sessions where session_id = ?", (row["session_id"],)).fetchone()
    conn.close()
    if not row:
        return None
    value = dict(row)
    value["notes"] = json.loads(value["notes"])
    value.pop("token_hash", None)
    return value


def submit_review(token, notes):
    if not isinstance(notes, list) or not notes:
        raise ValueError("review notes must be a non-empty list")
    clean = []
    for note in notes[:50]:
        if not isinstance(note, dict):
            raise ValueError("each review note must be an object")
        scope = str(note.get("scope") or "experiment").strip()[:200]
        text = str(note.get("text") or "").strip()
        if not text:
            raise ValueError("each review note requires text")
        clean.append({"scope": scope, "text": text[:4000]})

    conn = db()
    row = conn.execute("select * from review_sessions where token_hash = ?", (_hash(token),)).fetchone()
    if not row:
        conn.rollback()
        conn.close()
        raise ValueError("invalid review session")
    from .runner import redact_secrets
    root = resolve_root(row["experiment_id"])
    clean = [
        {
            "scope": redact_secrets(note["scope"], root),
            "text": redact_secrets(note["text"], root),
        }
        for note in clean
    ]
    conn.execute("begin immediate")
    row = conn.execute("select * from review_sessions where token_hash = ?", (_hash(token),)).fetchone()
    if row["status"] != "waiting" or row["expires_at"] <= int(time.time()):
        if row["status"] == "waiting":
            conn.execute("update review_sessions set status = 'expired' where session_id = ?", (row["session_id"],))
            conn.commit()
        else:
            conn.rollback()
        conn.close()
        raise ValueError("review session is no longer accepting feedback")
    cursor = conn.execute(
        """update review_sessions set status = 'completed', notes = ?, completed_at = ?
           where session_id = ? and status = 'waiting'""",
        (json.dumps(clean), now(), row["session_id"]),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        raise ValueError("review session was already completed")
    conn.commit()
    conn.close()
    return review_session(token)


def wait_for_review(token, *, timeout=900, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = review_session(token)
        if not session or session["status"] != "waiting":
            return session
        time.sleep(interval)
    return review_session(token)
