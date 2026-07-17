"""Ephemeral server jobs; execution state belongs to the run ledger."""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .store import db
from .workspace import experiment_id, now


def recover_stranded(root, *, canceled=False, run_id=None):
    """Finalize ledger rows whose worker no longer exists."""
    try:
        from .runs import recover_stranded_runs
    except ImportError:  # Kept local while older project installs migrate.
        return []
    return recover_stranded_runs(root, canceled=canceled, run_id=run_id)


def _run_ids(root):
    try:
        conn = db(root)
        rows = conn.execute("select run_id from runs where experiment_id = ? order by rowid desc", (experiment_id(root),)).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


class RunManager:
    """Own at most one execution worker for one project."""

    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root)
        self.lock = threading.Lock()
        self.job = None

    def _discover_run_id(self):
        if not self.job or self.job.get("run_id"):
            return
        try:
            conn = db(self.workspace_root)
            row = conn.execute(
                """select r.run_id from runs r
                   join triggers t on t.trigger_id = r.trigger_id
                   where t.request_id = ? order by r.rowid desc limit 1""",
                (self.job["job_id"],),
            ).fetchone()
            conn.close()
            has_trigger_correlation = True
        except Exception:
            row = None
            has_trigger_correlation = False
        if row:
            self.job["run_id"] = row[0]
            return
        if has_trigger_correlation:
            return
        known = self.job["known_run_ids"]
        self.job["run_id"] = next(
            (run_id for run_id in _run_ids(self.workspace_root) if run_id not in known), None
        )

    def _payload(self):
        if not self.job:
            return {"active": False, "job": None}

        self._discover_run_id()
        returncode = self.job["proc"].poll()
        if returncode is not None and self.job["status"] in {"running", "canceling"}:
            canceled = self.job["status"] == "canceling"
            if self.job.get("run_id"):
                recover_stranded(
                    self.workspace_root,
                    canceled=canceled,
                    run_id=self.job["run_id"],
                )
            self._discover_run_id()
            self.job["status"] = "canceled" if canceled else ("success" if returncode == 0 else "failed")
            self.job["returncode"] = returncode
            self.job["ended_at"] = now()
            print(
                f"[autoexp] run {self.job.get('run_id') or self.job['job_id']} "
                f"{self.job['status']}",
                flush=True,
            )

        public_keys = (
            "job_id", "run_id", "pid", "status", "started_at", "ended_at",
            "returncode", "log_path",
        )
        return {
            "active": self.job["status"] in {"running", "canceling"},
            "job": {key: self.job.get(key) for key in public_keys},
        }

    def active(self):
        with self.lock:
            return self._payload()

    def start(self, run_id=None, snapshot_id=None, *, trigger_kind="ui", actor_name="autoexp-view"):
        with self.lock:
            current = self._payload()
            if current["active"]:
                return False, current

            job_id = uuid.uuid4().hex
            job_dir = self.workspace_root / "server" / "jobs"
            job_dir.mkdir(parents=True, exist_ok=True)
            log_path = job_dir / f"{job_id}.log"
            cmd = [
                sys.executable, "-m", "autoexp.jobs",
                "--root", str(self.workspace_root),
                "--trigger-kind", trigger_kind,
                "--actor-name", actor_name,
                "--request-id", job_id,
            ]
            if run_id:
                cmd.extend(("--run-id", run_id))
            if snapshot_id:
                cmd.extend(("--snapshot-id", snapshot_id))
            known_run_ids = set(_run_ids(self.workspace_root))
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    cmd,
                    cwd=self.workspace_root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.job = {
                "job_id": job_id,
                "run_id": None,
                "known_run_ids": known_run_ids,
                "pid": proc.pid,
                "proc": proc,
                "status": "running",
                "started_at": now(),
                "ended_at": None,
                "returncode": None,
                "log_path": str(log_path),
            }
            action = f"re-run of {run_id}" if run_id else (
                f"snapshot {snapshot_id}" if snapshot_id else "new run"
            )
            print(f"[autoexp] started {action}", flush=True)
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


def worker(argv=None):
    parser = argparse.ArgumentParser(description="Run one Autoexp server job")
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--trigger-kind", default="ui")
    parser.add_argument("--actor-name", default="autoexp-view")
    parser.add_argument("--request-id")
    args = parser.parse_args(argv)

    from .execution import execute

    run = execute(
        root=Path(args.root),
        run_id=args.run_id,
        snapshot_id=args.snapshot_id,
        trigger_kind=args.trigger_kind,
        actor_name=args.actor_name,
        request_id=args.request_id,
    )
    print(json.dumps({"run_id": run["run_id"], "status": run["status"]}))
    if run["status"] == "success":
        return 0
    return run.get("exit_code") or 1


if __name__ == "__main__":
    try:
        raise SystemExit(worker())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
