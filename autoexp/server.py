import json
import ipaddress
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .artifacts import artifact_content, artifact_detail, list_artifacts, read_log
from .jobs import RunManager, recover_stranded
from .reports import (
    list_milestones,
    mark_milestone,
    project_summary,
    read_project_report,
    report_instruction,
    write_project_report,
    write_report_instruction,
)
from .autoresearch import for_project as research_for_project
from .store import init_db, require_autoexp_git_repo
from .workspace import (
    is_project_root,
    list_registered_projects,
    project_id,
    project_mode,
    register_project,
    resolve_registered_project,
)
from .runtime import (
    list_runs,
    read_script_params,
    run_diff,
    run_overview,
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


def bounded_int(raw, default=0, minimum=0, maximum=1048576):
    try:
        return max(minimum, min(int(raw), maximum))
    except (TypeError, ValueError):
        return default


def open_project_path(path):
    """Open a registered project in the host's file manager."""
    path = Path(path).resolve()
    command = "open" if sys.platform == "darwin" else "explorer" if os.name == "nt" else "xdg-open"
    executable = shutil.which(command)
    if not executable:
        raise ValueError(f"file manager command is unavailable: {command}")
    subprocess.Popen(
        [executable, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class AutoexpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, default_project=None, allow_origins=None):
        super().__init__(server_address, handler_class)
        self.default_project = default_project
        self.managers = {}
        self.researchers = {}
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

    def research(self, root):
        key = project_id(root)
        if key not in self.researchers:
            self.researchers[key] = research_for_project(root)
        return self.researchers[key]


class AutoexpHandler(BaseHTTPRequestHandler):
    server_version = "AutoexpHTTP/0.2"

    # ------------------------------------------------------------------
    #  Method entry points
    # ------------------------------------------------------------------

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
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except sqlite3.IntegrityError as exc:
            self._json({"error": str(exc)}, 409)
        except (TypeError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
        except SystemExit:
            self._json({"error": "request could not be completed"}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    # ------------------------------------------------------------------
    #  GET routes
    # ------------------------------------------------------------------

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
        if path == "/api/preflight":
            from .execution import preflight_request

            return self._json(preflight_request(
                root,
                run_id=query.get("run_id", [None])[0],
                snapshot_id=query.get("snapshot_id", [None])[0],
            ))
        if path == "/api/runs":
            return self._json({"runs": list_runs(limit, root)})
        if path.startswith("/api/runs/"):
            parts = [unquote(part) for part in path.removeprefix("/api/runs/").split("/")]
            run_id = parts[0]
            if not run_id:
                return self._json({"error": "run_id is required"}, 400)
            if len(parts) == 1:
                return self._json(run_overview(run_id, root))
            if parts[1:] == ["source"]:
                return self._json(run_source(run_id, root))
            if parts[1:] == ["artifacts"]:
                category = query.get("category", [None])[0]
                return self._json({
                    "run_id": run_id,
                    "artifacts": list_artifacts(run_id, root=root, category=category),
                })
            if len(parts) == 3 and parts[1] == "artifacts":
                return self._json(artifact_detail(run_id, parts[2], root))
            if len(parts) == 4 and parts[1] == "artifacts" and parts[3] == "content":
                offset = bounded_int(query.get("offset", [0])[0], maximum=2**63 - 1)
                size = bounded_int(
                    query.get("limit", [16 * 1024 * 1024])[0],
                    default=16 * 1024 * 1024,
                    minimum=1,
                    maximum=16 * 1024 * 1024,
                )
                artifact, content = artifact_content(
                    run_id, parts[2], root, offset=offset, limit=size
                )
                return self._content(
                    content,
                    artifact.get("media_type") or "application/octet-stream",
                    artifact.get("path"),
                    truncated=offset + len(content) < artifact["size_bytes"],
                )
            if len(parts) == 3 and parts[1] == "logs":
                offset = bounded_int(query.get("offset", [0])[0], maximum=2**63 - 1)
                size = bounded_int(query.get("limit", [65536])[0], default=65536, minimum=1)
                return self._json(read_log(run_id, parts[2], offset=offset, limit=size, root=root))
            if parts[1:] == ["report"]:
                return self._json(run_report(run_id, root))
            if parts[1:] == ["diff"]:
                return self._json(run_diff(
                    run_id,
                    root,
                    base_run_id=query.get("base_run_id", [None])[0],
                    base_snapshot_id=query.get("base_snapshot_id", [None])[0],
                ))
            return self._json({"error": "not found"}, 404)
        if path == "/api/status":
            return self._json({"run": manager.active(), "runs": list_runs(limit, root)})
        if path == "/api/script/params":
            return self._json(read_script_params(root))
        if path == "/api/report/instruction":
            return self._json(report_instruction(root))
        if path == "/api/project-report":
            return self._json(read_project_report(root))
        if path == "/api/project-summary":
            return self._json(project_summary(root, limit))
        if path == "/api/milestones":
            return self._json({"milestones": list_milestones(root)})
        if path == "/api/research":
            return self._json(self.server.research(root).state())
        if path == "/api/research/preflight":
            return self._json(self.server.research(root).preflight())
        if path == "/api/research/diff":
            attempt_id = query.get("attempt_id", query.get("tag", [""]))[0]
            if not attempt_id:
                return self._json({"error": "attempt_id is required"}, 400)
            return self._json(self.server.research(root).diff(attempt_id))
        if path == "/api/research/file":
            rel = query.get("path", [""])[0]
            if not rel:
                return self._json({"error": "path is required"}, 400)
            return self._json(self.server.research(root).open_file(rel))
        if path == "/api/run/source":
            return self._run_scoped(query, root, run_source)
        if path == "/api/run/report":
            return self._run_scoped(query, root, run_report)
        if path == "/api/run/log":
            tail = clamp(query.get("tail_bytes", [65536])[0], default=65536, maximum=1048576)
            log = self.server.research(root).log(tail) if project_mode(root) == "autoresearch" else manager.log(tail)
            return self._json({"log": log, "kind": "agent" if project_mode(root) == "autoresearch" else "job"})
        self._static(path)

    def _run_scoped(self, query, root, fn):
        run_id = query.get("run_id", [""])[0]
        if not run_id:
            return self._json({"error": "run_id is required"}, 400)
        return self._json(fn(run_id, root))

    # ------------------------------------------------------------------
    #  POST / PUT / PATCH routes
    # ------------------------------------------------------------------

    def _post(self):
        path = urlparse(self.path).path
        body = self._body({})

        if path == "/api/open-path":
            root = self._project_root(body)
            open_project_path(root)
            print(f"[autoexp] opened {root}", flush=True)
            return self._json({"ok": True, "path": str(root)})

        if path == "/api/milestones":
            if not isinstance(body, dict):
                return self._json({"error": "body must be a JSON object"}, 400)
            return self._json(mark_milestone(
                title=body.get("title"),
                significance=body.get("significance"),
                run_id=body.get("run_id"),
                attempt_id=body.get("attempt_id"),
                actor_name="autoexp-view",
                root=self._project_root(body),
            ), 201)

        if path == "/api/runs":
            if not isinstance(body, dict):
                return self._json({"error": "body must be a JSON object"}, 400)
            snapshot_id = body.get("snapshot_id")
            if snapshot_id is not None and not isinstance(snapshot_id, str):
                return self._json({"error": "snapshot_id must be a string"}, 400)
            root = self._project_root(body)
            if not self._execution_preflight(root, snapshot_id=snapshot_id):
                return
            ok, payload = self.server.manager(root).start(snapshot_id=snapshot_id)
            return self._json(payload, 202 if ok else 409)
        if path.startswith("/api/runs/"):
            parts = [unquote(part) for part in path.removeprefix("/api/runs/").split("/")]
            if len(parts) == 2 and parts[0] and parts[1] == "rerun":
                if not isinstance(body, dict):
                    return self._json({"error": "body must be a JSON object"}, 400)
                root = self._project_root(body)
                if not self._execution_preflight(root, run_id=parts[0]):
                    return
                ok, payload = self.server.manager(root).start(run_id=parts[0])
                return self._json(payload, 202 if ok else 409)
            if len(parts) == 2 and parts[0] and parts[1] == "cancel":
                if not isinstance(body, dict):
                    return self._json({"error": "body must be a JSON object"}, 400)
                manager = self.server.manager(self._project_root(body))
                current = manager.active()
                if not current["active"] or current["job"].get("run_id") != parts[0]:
                    return self._json({"error": "run is not active"}, 409)
                ok, payload = manager.kill(bool(body.get("force")))
                return self._json(payload, 202 if ok else 409)
            return self._json({"error": "not found"}, 404)
        if path == "/api/run/start":
            root = self._project_root(body)
            run_id = body.get("run_id")
            if run_id is not None and not isinstance(run_id, str):
                return self._json({"error": "run_id must be a string"}, 400)
            snapshot_id = body.get("snapshot_id")
            if snapshot_id is not None and not isinstance(snapshot_id, str):
                return self._json({"error": "snapshot_id must be a string"}, 400)
            if not self._execution_preflight(root, run_id=run_id, snapshot_id=snapshot_id):
                return
            ok, payload = self.server.manager(root).start(run_id, snapshot_id)
            return self._json(payload, 202 if ok else 409)
        if path == "/api/run/kill":
            root = self._project_root(body)
            ok, payload = self.server.manager(root).kill(bool(body.get("force")))
            return self._json(payload, 202 if ok else 409)
        if path == "/api/research/loop/start":
            root = self._project_root(body)
            research = self.server.research(root)
            preflight = research.preflight()
            if not preflight["ok"]:
                failed = next(
                    (item for item in preflight["checks"] if item["required"] and not item["ok"]),
                    None,
                )
                return self._json({
                    "error": (failed or {}).get("detail") or "research preflight failed",
                    "preflight": preflight,
                }, 422)
            result = research.start_loop()
            print("[autoexp] Autoresearch loop started", flush=True)
            return self._json(result, 202)
        if path == "/api/research/loop/kill":
            root = self._project_root(body)
            result = self.server.research(root).stop_loop()
            print("[autoexp] Autoresearch loop stop requested", flush=True)
            return self._json(result, 202)
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
        snapshot_id = body.get("snapshot_id")
        if snapshot_id is not None and not isinstance(snapshot_id, str):
            return self._json({"error": "snapshot_id must be a string"}, 400)
        save_as = body.get("save_as")
        if save_as is not None and not isinstance(save_as, str):
            return self._json({"error": "save_as must be a string"}, 400)

        result = save_script_file(
            rel,
            text,
            self._project_root(body),
            source_run_id=run_id,
            save_as=save_as,
            source_snapshot_id=snapshot_id,
            trigger_kind="ui",
            actor_name="autoexp-view",
        )
        print(f"[autoexp] saved {result['path']} as {result['snapshot']['snapshot_id']}", flush=True)
        self._json(result)

    def _put(self):
        path = urlparse(self.path).path

        if path == "/api/research/file":
            body = self._body({})
            rel = body.get("path") if isinstance(body, dict) else None
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(rel, str) or not rel:
                return self._json({"error": "path is required"}, 400)
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            result = self.server.research(self._project_root(body)).save_file(rel, text)
            print(f"[autoexp] saved research file {rel}", flush=True)
            return self._json(result)

        if path == "/api/report/instruction":
            body = self._body({})
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            result = write_report_instruction(text, self._project_root(body))
            print("[autoexp] saved report guidance", flush=True)
            return self._json(result)

        if path == "/api/project-report":
            body = self._body({})
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            result = write_project_report(text, self._project_root(body))
            print("[autoexp] saved project report", flush=True)
            return self._json(result)

        if path == "/api/research/program":
            body = self._body({})
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            result = self.server.research(self._project_root(body)).save_program(text)
            print("[autoexp] saved research program", flush=True)
            return self._json(result)

        if path == "/api/research/subject":
            body = self._body({})
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            research = self.server.research(self._project_root(body))
            if not research.can_import_baseline():
                return self._json({"error": "baseline import is only available before attempts and before candidate.py is edited"}, 409)
            result = research.save_subject(text)
            print("[autoexp] imported Autoresearch baseline", flush=True)
            return self._json(result)

        if path != "/api/script/params":
            return self._json({"error": "not found"}, 404)

        body = self._body()
        params = body.get("params") if isinstance(body, dict) and "params" in body else body
        if not isinstance(params, dict):
            return self._json({"error": "params must be a JSON object"}, 400)

        root_data = body if isinstance(body, dict) else {}
        result = write_script_params(
            params,
            self._project_root(root_data),
            trigger_kind="ui",
            actor_name="autoexp-view",
        )
        print(f"[autoexp] saved parameters as {result['snapshot']['snapshot_id']}", flush=True)
        self._json(result)

    # ------------------------------------------------------------------
    #  Shared helpers
    # ------------------------------------------------------------------

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

    def _execution_preflight(self, root, run_id=None, snapshot_id=None):
        from .execution import preflight_request

        result = preflight_request(root, run_id=run_id, snapshot_id=snapshot_id)
        if result["ok"]:
            return True
        failed = next((item for item in result["checks"] if not item["ok"]), None)
        self._json({
            "error": (failed or {}).get("detail") or "execution preflight failed",
            "preflight": result,
        }, 422)
        return False

    def _body(self, default=None):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return default
        if length > 16 * 1024 * 1024:
            raise ValueError("request body is too large")
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin or origin in self.server.allow_origins:
            return True
        parsed = urlparse(origin)
        try:
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme != "http" or port != self.server.server_port:
            return False
        hostname = parsed.hostname or ""
        if hostname == "localhost":
            return True
        try:
            address = ipaddress.ip_address(hostname)
            bound = ipaddress.ip_address(self.server.server_address[0])
        except ValueError:
            return False
        return address.is_loopback or (
            not bound.is_unspecified and address == bound
        )

    def _json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self._headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _content(self, body, content_type, path=None, *, truncated=False):
        self.send_response(200)
        self._headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.send_header("X-Content-Truncated", "true" if truncated else "false")
        if content_type.split(";", 1)[0].lower() in {"text/html", "application/xhtml+xml"}:
            filename = Path(path or "artifact").name.replace('"', "").replace("\r", "").replace("\n", "")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _static(self, path):
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (UI_DIR / rel).resolve()

        if not target.is_relative_to(UI_DIR.resolve()) or not target.is_file():
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
        status = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 0
        if status >= 400:
            print(f"[autoexp] HTTP {status}: {args[0]}", file=sys.stderr, flush=True)


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
            recover_stranded(root)

    server = AutoexpHTTPServer(
        (host, port),
        AutoexpHandler,
        default_project=default_project,
        allow_origins=allow_origins,
    )
    print(f"[autoexp] view ready at http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[autoexp] view stopped", flush=True)
    finally:
        server.server_close()
