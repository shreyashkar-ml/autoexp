import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .reports import report_instruction, write_report_instruction
from .store import init_db, require_autoexp_git_repo
from .workspace import (
    is_project_root,
    list_registered_projects,
    now,
    project_id,
    register_project,
    resolve_registered_project,
)
from .runtime import (
    list_runs,
    read_script_params,
    run_report,
    run_source,
    save_script_file,
    workspace,
    write_script_params,
)


UI_DIR = Path(__file__).with_name("ui")


def clamp(raw, default=20, maximum=200):
    """Coerce a query value to an int in [1, maximum], falling back to default."""
    try:
        return max(1, min(int(raw), maximum))
    except (TypeError, ValueError):
        return default


# --- run lifecycle: one background `autoexp run` per project ----------------

class RunManager:
    """Owns at most one running experiment subprocess for a single project."""

    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root)
        self.lock = threading.Lock()
        self.job = None

    def _payload(self):
        """Snapshot of the current job, refreshing terminal status from the process."""
        if not self.job:
            return {"active": False, "job": None}

        returncode = self.job["proc"].poll()
        if returncode is not None and self.job["status"] in {"running", "canceling"}:
            if self.job["status"] == "canceling":
                self.job["status"] = "canceled"
            else:
                self.job["status"] = "success" if returncode == 0 else "failed"
            self.job["returncode"] = returncode
            self.job["ended_at"] = now()

        public_keys = ("job_id", "pid", "status", "started_at", "ended_at", "returncode", "log_path")
        return {
            "active": self.job["status"] in {"running", "canceling"},
            "job": {key: self.job[key] for key in public_keys},
        }

    def active(self):
        with self.lock:
            return self._payload()

    def start(self, run_id=None):
        with self.lock:
            current = self._payload()
            if current["active"]:
                return False, current

            job_id = uuid.uuid4().hex
            job_dir = self.workspace_root / "server" / "jobs"
            job_dir.mkdir(parents=True, exist_ok=True)
            log_path = job_dir / f"{job_id}.log"
            cmd = [sys.executable, "-m", "autoexp", "run", *([run_id] if run_id else [])]
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    cmd, cwd=self.workspace_root, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.job = {
                "job_id": job_id,
                "pid": proc.pid,
                "proc": proc,
                "status": "running",
                "started_at": now(),
                "ended_at": None,
                "returncode": None,
                "log_path": str(log_path),
            }
            return True, self._payload()

    def kill(self, force=False):
        with self.lock:
            current = self._payload()
            if not current["active"]:
                return False, current

            sig = signal.SIGKILL if force else signal.SIGTERM
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(self.job["pid"]), sig)
                else:
                    self.job["proc"].send_signal(sig)
            except ProcessLookupError:
                return False, self._payload()

            self.job["status"] = "canceling"
            return True, self._payload()

    def log(self, tail_bytes=65536):
        with self.lock:
            path = Path(self.job["log_path"]) if self.job else None
        if not path or not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            return handle.read().decode(errors="replace")


class AutoexpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, default_project=None, allow_origins=None):
        super().__init__(server_address, handler_class)
        self.default_project = default_project
        self.managers = {}
        self.allow_origins = set(allow_origins or [])

    def project_root(self, raw=None):
        return resolve_registered_project(raw or self.default_project)

    def selected_project_id(self, raw=None):
        return project_id(self.project_root(raw))

    def manager(self, root):
        key = project_id(root)
        if key not in self.managers:
            self.managers[key] = RunManager(root)
        return self.managers[key]


class AutoexpHandler(BaseHTTPRequestHandler):
    server_version = "AutoexpHTTP/0.1"

    # --- method entry points -------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post, check_origin=True)

    def do_PUT(self):
        self._dispatch(self._put, check_origin=True)

    def do_PATCH(self):
        self._dispatch(self._patch, check_origin=True)

    def _dispatch(self, handler, check_origin=False):
        if check_origin and not self._origin_allowed():
            self._json({"error": "origin not allowed"}, 403)
            return
        try:
            handler()
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    # --- GET routes ----------------------------------------------------------

    def _get(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/health":
            return self._json({"ok": True})
        if path == "/api/projects":
            raw = query.get("project_id", [""])[0] or None
            projects = list_registered_projects()
            has_existing = any(item["exists"] for item in projects)
            selected = self.server.selected_project_id(raw) if has_existing else None
            return self._json({"projects": projects, "selected_project_id": selected})
        if not path.startswith("/api/"):
            return self._static(path)

        root = self._project_root(query)
        manager = self.server.manager(root)
        limit = clamp(query.get("limit", [20])[0])

        if path == "/api/workspace":
            return self._json(workspace(root))
        if path == "/api/runs":
            return self._json({"runs": list_runs(limit, root)})
        if path == "/api/status":
            return self._json({"run": manager.active(), "runs": list_runs(limit, root)})
        if path == "/api/script/params":
            return self._json(read_script_params(root))
        if path == "/api/report/instruction":
            return self._json(report_instruction(root))
        if path == "/api/run/source":
            return self._run_scoped(query, root, run_source)
        if path == "/api/run/report":
            return self._run_scoped(query, root, run_report)
        if path == "/api/run/log":
            tail = clamp(query.get("tail_bytes", [65536])[0], default=65536, maximum=1048576)
            return self._json({"log": manager.log(tail)})
        self._static(path)

    def _run_scoped(self, query, root, fn):
        run_id = query.get("run_id", [""])[0]
        if not run_id:
            return self._json({"error": "run_id is required"}, 400)
        return self._json(fn(run_id, root))

    # --- POST/PUT/PATCH routes ----------------------------------------------

    def _post(self):
        path = urlparse(self.path).path
        body = self._body({})

        if path == "/api/run/start":
            root = self._project_root(body)
            run_id = body.get("run_id")
            if run_id is not None and not isinstance(run_id, str):
                return self._json({"error": "run_id must be a string"}, 400)
            ok, payload = self.server.manager(root).start(run_id)
            return self._json(payload, 202 if ok else 409)
        if path == "/api/run/kill":
            root = self._project_root(body)
            ok, payload = self.server.manager(root).kill(bool(body.get("force")))
            return self._json(payload, 202 if ok else 409)
        self._json({"error": "not found"}, 404)

    def _patch(self):
        if urlparse(self.path).path != "/api/script/file":
            return self._json({"error": "not found"}, 404)

        body = self._body({})
        rel = body.get("path")
        text = body.get("text")
        if not isinstance(rel, str) or not rel:
            return self._json({"error": "path is required"}, 400)
        if not isinstance(text, str):
            return self._json({"error": "text must be a string"}, 400)

        run_id = body.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return self._json({"error": "run_id must be a string"}, 400)
        save_as = body.get("save_as")
        if save_as is not None and not isinstance(save_as, str):
            return self._json({"error": "save_as must be a string"}, 400)

        self._json(save_script_file(rel, text, self._project_root(body), run_id, save_as))

    def _put(self):
        path = urlparse(self.path).path

        if path == "/api/report/instruction":
            body = self._body({})
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            return self._json(write_report_instruction(text, self._project_root(body)))

        if path != "/api/script/params":
            return self._json({"error": "not found"}, 404)

        body = self._body()
        params = body.get("params") if isinstance(body, dict) and "params" in body else body
        if not isinstance(params, dict):
            return self._json({"error": "params must be a JSON object"}, 400)

        root_data = body if isinstance(body, dict) else {}
        self._json(write_script_params(params, self._project_root(root_data)))

    # --- shared helpers ------------------------------------------------------

    @staticmethod
    def _project_id_from(data):
        """Pull project_id from a request, handling both query (list) and JSON (scalar) shapes."""
        if not isinstance(data, dict):
            return None
        value = data.get("project_id")
        if isinstance(value, list):
            return value[0] if value else None
        return value

    def _project_root(self, data):
        return self.server.project_root(self._project_id_from(data) or None)

    def _body(self, default=None):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return default
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        return not origin or (host and origin == f"http://{host}") or origin in self.server.allow_origins

    def _json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self._headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self):
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _static(self, path):
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (UI_DIR / rel).resolve()

        if not str(target).startswith(str(UI_DIR.resolve())) or not target.is_file():
            target = UI_DIR / "index.html"
        if not target.is_file():
            return self._json({"error": "not found"}, 404)

        body = target.read_bytes()
        if target.suffix == ".jsx":
            content_type = "text/javascript"
        else:
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self._headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def view(host, port, allow_origins=None, project=None):
    default_project = None
    if project:
        root = Path(project).expanduser()
        if is_project_root(root):
            default_project = register_project(root)["project_id"]
        else:
            default_project = project

    for item in list_registered_projects():
        if item["exists"]:
            root = Path(item["path"])
            require_autoexp_git_repo(root)
            init_db(root)

    server = AutoexpHTTPServer(
        (host, port),
        AutoexpHandler,
        default_project=default_project,
        allow_origins=allow_origins,
    )
    print(f"serving Autoexp view on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
