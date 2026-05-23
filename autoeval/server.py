import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .cli import (
    STAGES,
    compute_hashes,
    db,
    ensure_autoeval_git_repo,
    ensure_workspace,
    ensure_workspace_contract,
    git_commit_storage,
    init_db,
    now,
    project_root,
    read_json,
    upsert_stage_versions,
    write_json,
)


def clamp_limit(raw, default=20, maximum=200):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default

    return max(1, min(value, maximum))


def json_or_none(value):
    if value is None:
        return None

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def run_row(row):
    return {
        "run_id": row["run_id"],
        "status": row["status"],
        "capsule_hash": row["capsule_hash"],
        "input_hash": row["input_hash"],
        "script_hash": row["script_hash"],
        "report_hash": row["report_hash"],
        "git_commit": row["git_commit"],
        "stage_status": json_or_none(row["stage_status"]),
        "created_at": row["created_at"],
    }


def version_row(stage, row):
    hash_key = f"{stage}_hash"

    return {
        "stage": stage,
        "hash": row[hash_key],
        "created_at": row["created_at"],
        "git_commit": row["git_commit"],
        "label": row["label"],
        "metadata": json_or_none(row["metadata_json"]),
    }


def list_runs(limit=20, root=None):
    conn = db(root)
    rows = conn.execute(
        """
        select
            run_id,
            status,
            capsule_hash,
            input_hash,
            script_hash,
            report_hash,
            git_commit,
            stage_status,
            created_at
        from runs
        order by created_at desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    return [run_row(row) for row in rows]


def list_versions(limit=50, root=None):
    conn = db(root)
    data = {}

    for stage in ("input", "script", "report"):
        rows = conn.execute(
            f"""
            select
                {stage}_hash,
                created_at,
                git_commit,
                label,
                metadata_json
            from {stage}_versions
            order by created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        data[stage] = [version_row(stage, row) for row in rows]

    conn.close()
    return data


class RunManager:
    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root)
        self.lock = threading.Lock()
        self.job = None

    def _job_payload(self):
        if not self.job:
            return {
                "active": False,
                "job": None,
            }

        proc = self.job["proc"]
        returncode = proc.poll()

        if returncode is not None and self.job["status"] in {"running", "paused", "canceling"}:
            if self.job["status"] == "canceling":
                self.job["status"] = "canceled"
            else:
                self.job["status"] = "success" if returncode == 0 else "failed"

            self.job["returncode"] = returncode
            self.job["ended_at"] = self._now()

        return {
            "active": self.job["status"] in {"running", "paused", "canceling"},
            "job": {
                "job_id": self.job["job_id"],
                "pid": proc.pid,
                "status": self.job["status"],
                "started_at": self.job["started_at"],
                "ended_at": self.job["ended_at"],
                "returncode": self.job["returncode"],
                "log_path": str(self.job["log_path"]),
            },
        }

    def active(self):
        with self.lock:
            return self._job_payload()

    def start(self, force=False):
        with self.lock:
            current = self._job_payload()

            if current["active"]:
                return False, current

            job_id = uuid.uuid4().hex
            server_dir = self.workspace_root / "server" / "jobs"
            server_dir.mkdir(parents=True, exist_ok=True)
            log_path = server_dir / f"{job_id}.log"
            log = log_path.open("w")

            cmd = [sys.executable, "-m", "autoeval", "run"]

            if force:
                cmd.append("--force")

            proc = subprocess.Popen(
                cmd,
                cwd=self.workspace_root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log.close()

            self.job = {
                "job_id": job_id,
                "proc": proc,
                "status": "running",
                "started_at": self._now(),
                "ended_at": None,
                "returncode": None,
                "log_path": log_path,
            }

            return True, self._job_payload()

    def pause(self):
        if not hasattr(signal, "SIGSTOP"):
            return False, self.active()

        return self._send_signal("paused", signal.SIGSTOP)

    def resume(self):
        if not hasattr(signal, "SIGCONT"):
            return False, self.active()

        return self._send_signal("running", signal.SIGCONT)

    def kill(self, force=False):
        sig = signal.SIGKILL if force else signal.SIGTERM
        return self._send_signal("canceling", sig)

    def log(self, tail_bytes=65536):
        with self.lock:
            if not self.job:
                return ""

            path = self.job["log_path"]

        if not path.exists():
            return ""

        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            return handle.read().decode(errors="replace")

    def _send_signal(self, next_status, sig):
        with self.lock:
            current = self._job_payload()

            if not current["active"]:
                return False, current

            proc = self.job["proc"]

            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), sig)
                else:
                    proc.send_signal(sig)
            except ProcessLookupError:
                return False, self._job_payload()

            self.job["status"] = next_status
            return True, self._job_payload()

    def _now(self):
        return __import__("time").strftime("%Y-%m-%dT%H-%M-%SZ", __import__("time").gmtime())


class AutoevalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, manager, allow_origins=None):
        super().__init__(server_address, handler_class)
        self.manager = manager
        self.allow_origins = set(allow_origins or [])


class AutoevalHandler(BaseHTTPRequestHandler):
    server_version = "AutoevalHTTP/0.1"

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            self._handle_get()
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def do_POST(self):
        if not self._origin_allowed():
            self._json({"error": "origin not allowed"}, status=403)
            return

        try:
            self._handle_post()
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def do_PUT(self):
        if not self._origin_allowed():
            self._json({"error": "origin not allowed"}, status=403)
            return

        try:
            self._handle_put()
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def _handle_get(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/health":
            self._json({"ok": True})
            return

        if parsed.path == "/api/workspace":
            root = project_root()
            self._json(
                {
                    "root": str(root),
                }
            )
            return

        if parsed.path == "/api/runs":
            limit = clamp_limit(query.get("limit", [20])[0])
            self._json({"runs": list_runs(limit, root=project_root())})
            return

        if parsed.path == "/api/status":
            limit = clamp_limit(query.get("limit", [20])[0])
            self._json(
                {
                    "run": self.server.manager.active(),
                    "runs": list_runs(limit, root=project_root()),
                }
            )
            return

        if parsed.path == "/api/versions":
            limit = clamp_limit(query.get("limit", [50])[0], default=50)
            self._json({"versions": list_versions(limit, root=project_root())})
            return

        if parsed.path == "/api/input/params":
            self._json(read_input_params())
            return

        if parsed.path == "/api/run/active":
            self._json(self.server.manager.active())
            return

        if parsed.path == "/api/run/log":
            tail = clamp_limit(query.get("tail_bytes", [65536])[0], default=65536, maximum=1048576)
            self._json({"log": self.server.manager.log(tail)})
            return

        self._json({"error": "not found"}, status=404)

    def _handle_post(self):
        parsed = urlparse(self.path)
        body = self._read_json_body(default={})

        if parsed.path == "/api/run/start":
            started, payload = self.server.manager.start(force=bool(body.get("force")))
            self._json(payload, status=202 if started else 409)
            return

        if parsed.path == "/api/run/pause":
            ok, payload = self.server.manager.pause()
            self._json(payload, status=202 if ok else 409)
            return

        if parsed.path == "/api/run/resume":
            ok, payload = self.server.manager.resume()
            self._json(payload, status=202 if ok else 409)
            return

        if parsed.path == "/api/run/kill":
            ok, payload = self.server.manager.kill(force=bool(body.get("force")))
            self._json(payload, status=202 if ok else 409)
            return

        if parsed.path == "/api/storage":
            label = body.get("label")
            message = body.get("message") or "autoeval storage"
            root = project_root()
            hashes = compute_hashes(root)
            created_at = now()
            commit, committed = git_commit_storage(message, root=root)
            inserted = upsert_stage_versions(hashes, commit, created_at, label=label, root=root)

            self._json(
                {
                    "storage_commit": commit,
                    "committed": committed,
                    "hashes": hashes,
                    "versions": {
                        stage: "stored" if inserted[stage] else "existing"
                        for stage in STAGES
                    },
                },
                status=201 if committed else 200,
            )
            return

        self._json({"error": "not found"}, status=404)

    def _handle_put(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/input/params":
            body = self._read_json_body()

            if not isinstance(body, dict):
                self._json({"error": "request body must be a JSON object"}, status=400)
                return

            params = body.get("params") if set(body) == {"params"} else body

            if not isinstance(params, dict):
                self._json({"error": "params must be a JSON object"}, status=400)
                return

            write_json(project_root() / "input" / "params.json", params)
            self._json(read_input_params())
            return

        self._json({"error": "not found"}, status=404)

    def _read_json_body(self, default=None):
        length = int(self.headers.get("Content-Length", "0"))

        if length == 0:
            return default

        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _origin_allowed(self):
        origin = self.headers.get("Origin")

        if not origin:
            return True

        host = self.headers.get("Host")
        same_origin = host and origin == f"http://{host}"

        return same_origin or origin in self.server.allow_origins

    def _json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_common_headers(self):
        self.send_header("Cache-Control", "no-store")

        origin = self.headers.get("Origin")

        if origin and (origin in self.server.allow_origins or self._origin_allowed()):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def read_input_params():
    root = project_root()
    schema_path = root / "input" / "params.schema.json"
    params_path = root / "input" / "params.json"

    return {
        "schema": read_json(schema_path) if schema_path.exists() else None,
        "params": read_json(params_path) if params_path.exists() else None,
    }


def serve(host, port, allow_origins=None):
    root = ensure_workspace()
    ensure_workspace_contract(root)
    ensure_autoeval_git_repo(root)
    init_db(root)

    manager = RunManager(root)
    server = AutoevalHTTPServer(
        (host, port),
        AutoevalHandler,
        manager=manager,
        allow_origins=allow_origins,
    )

    print(f"serving Autoeval API on http://{host}:{server.server_port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
