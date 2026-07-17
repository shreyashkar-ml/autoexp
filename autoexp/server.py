"""Global read/download dashboard and explicit browser-review transport."""

import io
import hashlib
import ipaddress
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import urlopen

from .artifacts import artifact_content, artifact_detail, artifact_file, list_artifacts, read_log
from .reports import list_documents, list_milestones, read_project_report
from .review import review_session, submit_review
from .runtime import list_runs, run_diff, run_overview, run_report, run_source
from .snapshots import materialize_snapshot
from .store import db
from .workspace import (
    experiment_entry, manifest_files, project_mode, registry, resolve_root,
    safe_repository_path,
)


UI_DIR = Path(__file__).with_name("ui")


def clamp(raw, default=20, maximum=200):
    try:
        return max(1, min(int(raw), maximum))
    except (TypeError, ValueError):
        return default


def bounded_int(raw, default=0, minimum=0, maximum=16 * 1024 * 1024):
    try:
        return max(minimum, min(int(raw), maximum))
    except (TypeError, ValueError):
        return default


def _run_stats():
    conn = db()
    rows = conn.execute(
        "select experiment_id, run_id, status, created_at from runs order by rowid desc"
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        item = result.setdefault(row["experiment_id"], {"run_count": 0})
        item["run_count"] += 1
        if "latest_run_id" not in item:
            item.update(
                latest_run_id=row["run_id"], latest_run_status=row["status"],
                latest_run_at=row["created_at"],
            )
    return result


def _public_experiment(item, stats=None):
    value = {key: item.get(key) for key in (
        "experiment_id", "repo_id", "repo_title", "repo_path", "title", "objective",
        "kind", "status", "runner", "created_at", "updated_at", "exists",
    )}
    return value | (stats or {"run_count": 0})


def _experiment_payload(root, limit=100):
    item = experiment_entry(root)
    payload = {
        "experiment": _public_experiment(item),
        "files": manifest_files(root, refresh=False),
        "runs": list_runs(limit, root),
        "documents": list_documents(root),
        "milestones": list_milestones(root),
        "project_report": read_project_report(root),
        "managed": {
            "stage": item["stage"],
            "params": item["params"],
            "params_schema": item["params_schema"],
            "report_guidance": item["report_guidance"],
        },
    }
    if project_mode(root) == "autoresearch":
        from .autoresearch import for_project
        payload["research"] = for_project(root).state()
    return payload


def _bundle(root):
    root = resolve_root(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for top in ("reports", "insights", "runs"):
            base = root / top
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(root))
    return buffer.getvalue()


class AutoexpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, allow_origins=None):
        super().__init__(address, handler)
        self.allow_origins = set(allow_origins or [])


class AutoexpHandler(BaseHTTPRequestHandler):
    server_version = "AutoexpHTTP/0.3"

    def do_OPTIONS(self):
        self.send_response(204)
        self._headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post, check_origin=True)

    def _dispatch(self, fn, check_origin=False):
        if check_origin and not self._origin_allowed():
            return self._json({"error": "origin not allowed"}, 403)
        try:
            fn()
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except (TypeError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def _get(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/health":
            return self._json({"ok": True, "version": "0.3"})
        if path == "/api/registry":
            stats = _run_stats()
            repos = registry()
            for repo in repos:
                repo["experiments"] = [_public_experiment(item, stats.get(item["experiment_id"])) for item in repo["experiments"]]
            return self._json({"repositories": repos})
        if path == "/api/review":
            token = query.get("token", [""])[0]
            session = review_session(token)
            return self._json({"session": session}, 200 if session else 404)
        if not path.startswith("/api/"):
            return self._static(path)

        parts = [unquote(part) for part in path.removeprefix("/api/").split("/") if part]
        if len(parts) >= 2 and parts[0] == "experiments":
            root = resolve_root(parts[1])
            if len(parts) == 2:
                return self._json(_experiment_payload(root, clamp(query.get("limit", [100])[0])))
            if parts[2:] == ["files"]:
                return self._experiment_file(root, query)
            if parts[2:] == ["bundle"]:
                entry = experiment_entry(root)
                return self._content(_bundle(root), "application/zip", f"{entry['experiment_id']}.zip", attachment=True)
            if parts[2:] == ["documents"]:
                return self._document(root, query)

        if len(parts) >= 2 and parts[0] == "runs":
            run_id = parts[1]
            root = self._root_for_run(run_id)
            if len(parts) == 2:
                return self._json(run_overview(run_id, root))
            if parts[2:] == ["source"]:
                return self._json(run_source(run_id, root))
            if parts[2:] == ["report"]:
                report = run_report(run_id, root)
                if query.get("download", ["0"])[0] == "1" and report["artifact"]:
                    return self._content(
                        report["text"].encode(), report["artifact"]["media_type"],
                        Path(report["path"]).name, attachment=True,
                    )
                return self._json(report)
            if parts[2:] == ["diff"]:
                return self._json(run_diff(run_id, root, base_run_id=query.get("base_run_id", [None])[0]))
            if parts[2:] == ["artifacts"]:
                return self._json({"artifacts": list_artifacts(run_id, root, query.get("category", [None])[0])})
            if len(parts) == 4 and parts[2] == "artifacts":
                return self._json(artifact_detail(run_id, parts[3], root))
            if len(parts) == 5 and parts[2] == "artifacts" and parts[4] == "content":
                if query.get("download", ["0"])[0] == "1":
                    artifact, file_path = artifact_file(run_id, parts[3], root)
                    return self._file(file_path, artifact["media_type"], Path(artifact["path"]).name)
                offset = bounded_int(query.get("offset", [0])[0])
                size = bounded_int(query.get("limit", [16 * 1024 * 1024])[0], minimum=1)
                artifact, content = artifact_content(run_id, parts[3], root, offset=offset, limit=size)
                return self._content(content, artifact["media_type"], Path(artifact["path"]).name)
            if len(parts) == 4 and parts[2] == "logs":
                log = read_log(run_id, parts[3], root=root)
                if query.get("download", ["0"])[0] == "1":
                    artifact = next(
                        (
                            item for item in list_artifacts(run_id, root, "log")
                            if item["path"] == f"logs/script.{log['stream']}.log"
                        ),
                        None,
                    )
                    filename = f"{run_id}-{log['stream']}.log"
                    if artifact:
                        _, file_path = artifact_file(run_id, artifact["artifact_id"], root)
                        return self._file(file_path, "text/plain; charset=utf-8", filename)
                    return self._content(b"", "text/plain; charset=utf-8", filename, attachment=True)
                return self._json(log)
        return self._json({"error": "not found"}, 404)

    def _post(self):
        if urlparse(self.path).path != "/api/review/submit":
            return self._json({"error": "not found"}, 404)
        body = self._body({})
        if not isinstance(body, dict):
            return self._json({"error": "body must be an object"}, 400)
        return self._json({"session": submit_review(body.get("token"), body.get("notes"))})

    def _root_for_run(self, run_id):
        from .store import db
        conn = db()
        row = conn.execute("select experiment_id from runs where run_id = ?", (run_id,)).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"unknown run_id: {run_id}")
        return resolve_root(row["experiment_id"])

    def _experiment_file(self, root, query):
        rel = query.get("path", [""])[0]
        item = next((item for item in manifest_files(root, refresh=False) if item["path"] == rel), None)
        if not item:
            raise ValueError(f"file is not declared: {rel}")
        if item["role"] == "secret-source":
            return self._json({"file": item, "text": None, "secret": True})
        snapshot_id = query.get("snapshot", [None])[0]
        if snapshot_id:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="autoexp-browser-source-") as tmp:
                materialize_snapshot(snapshot_id, tmp, root)
                path = Path(tmp) / rel
                text = path.read_text(errors="replace") if path.is_file() else None
            return self._json({"file": item, "text": text, "snapshot_id": snapshot_id, "live": False})
        path = safe_repository_path(root, rel)
        return self._json({"file": item, "text": path.read_text(errors="replace") if path.is_file() else None, "live": True})

    def _document(self, root, query):
        rel = query.get("path", [""])[0]
        document = next((item for item in list_documents(root) if item["path"] == rel), None)
        if not document:
            raise ValueError(f"unknown document: {rel}")
        path = (root / rel).resolve()
        if not path.is_file() or not path.is_relative_to(root.resolve()):
            raise FileNotFoundError(rel)
        if path.stat().st_size != document["size_bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != document["content_hash"]:
            raise ValueError(f"immutable document content changed: {rel}")
        return self._content(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.name, attachment=query.get("download", ["0"])[0] == "1")

    def _body(self, default=None):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return default
        if length > 1024 * 1024:
            raise ValueError("request body is too large")
        return json.loads(self.rfile.read(length).decode())

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin or origin in self.server.allow_origins:
            return True
        parsed = urlparse(origin)
        try:
            return parsed.scheme == "http" and parsed.port == self.server.server_port and (parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback)
        except (ValueError, TypeError):
            return False

    def _headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self._headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _content(self, body, content_type, filename=None, *, attachment=False, static=False):
        self.send_response(200)
        self._headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" if static else "sandbox; default-src 'none'")
        if attachment and filename:
            safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, content_type, filename):
        path = Path(path)
        safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
        self.send_response(200)
        self._headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def _static(self, path):
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (UI_DIR / rel).resolve()
        if not target.is_relative_to(UI_DIR.resolve()) or not target.is_file():
            target = UI_DIR / "index.html"
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        return self._content(target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream", static=True)

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).isdigit() and int(args[1]) >= 400:
            print(f"[autoexp] HTTP {args[1]}: {args[0]}", file=sys.stderr, flush=True)


def _require_loopback_host(host):
    try:
        loopback = host == "localhost" or (
            ipaddress.ip_address(host).version == 4
            and ipaddress.ip_address(host).is_loopback
        )
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("Autoexp view may only bind to a loopback host")


def _healthy(host, port):
    try:
        with urlopen(f"http://{host}:{port}/api/health", timeout=0.5) as response:
            data = json.load(response)
            return data.get("ok") is True and data.get("version") == "0.3"
    except Exception:
        return False


def ensure_server(host="127.0.0.1", port=8765):
    _require_loopback_host(host)
    if _healthy(host, port):
        return f"http://{host}:{port}", None
    proc = subprocess.Popen(
        [sys.executable, "-m", "autoexp", "view", "--host", host, "--port", str(port), "--no-open"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(40):
        if _healthy(host, port):
            return f"http://{host}:{port}", proc
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise ValueError("Autoexp browser server could not start")


def view(host="127.0.0.1", port=8765, allow_origins=None, experiment=None, review_token=None, open_browser=True):
    _require_loopback_host(host)
    if _healthy(host, port):
        base = f"http://{host}:{port}"
        query = []
        if experiment:
            query.append(f"experiment={quote(str(experiment))}")
        if review_token:
            query.append(f"review={quote(review_token)}")
        url = base + ("/?" + "&".join(query) if query else "")
        if open_browser:
            webbrowser.open(url)
        print(f"[autoexp] using {url}")
        return
    server = AutoexpHTTPServer((host, port), AutoexpHandler, allow_origins)
    query = []
    if experiment:
        query.append(f"experiment={quote(str(experiment))}")
    if review_token:
        query.append(f"review={quote(review_token)}")
    url = f"http://{host}:{server.server_port}/" + ("?" + "&".join(query) if query else "")
    print(f"[autoexp] view ready at {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[autoexp] view stopped", flush=True)
    finally:
        server.server_close()
