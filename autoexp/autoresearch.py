"""Durable Autoresearch contracts and attempts on Autoexp's shared execution plane."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .workspace import (
    EXPERIMENT_DIR,
    PARAMS_FILE,
    PARAMS_SCHEMA_FILE,
    PROJECT_CONFIG,
    STAGE_MANIFEST,
)


Role = Literal["human", "agent", "frozen"]
Direction = Literal["min", "max"]


@dataclass
class Objective:
    metric: str
    direction: Direction = "min"
    baseline: float | None = None
    budget_sec: int = 300


@dataclass
class FileRole:
    path: str
    role: Role
    desc: str = ""


@dataclass
class MetricSource:
    kind: Literal["json", "regex"] = "json"
    path: str = "metrics.json"
    key: str = ""


@dataclass
class ResearchConfig:
    objective: Objective
    files: list[FileRole]
    metric: MetricSource
    agent_cmd: list[str]

    @classmethod
    def load(cls, project_dir: Path):
        config = json.loads((project_dir / PROJECT_CONFIG).read_text())
        research = config["autoresearch"]
        return cls(
            objective=Objective(**research["objective"]),
            files=[FileRole(**item) for item in research["files"]],
            metric=MetricSource(**research.get("metric", {})),
            agent_cmd=research.get("agent", {}).get("cmd", ["codex"]),
        )

    def role_of(self, path):
        return next((item.role for item in self.files if item.path == path), None)

    def path_for(self, role):
        return next(item.path for item in self.files if item.role == role)

    @property
    def subject_path(self):
        return self.path_for("agent")

    @property
    def program_path(self):
        return self.path_for("human")

    @property
    def evaluator_path(self):
        return self.path_for("frozen")


class ResearchPreflightError(ValueError):
    def __init__(self, result):
        self.result = result
        failed = next(
            (item for item in result["checks"] if not item["ok"] and item["required"]),
            None,
        )
        super().__init__((failed or {}).get("detail") or "research preflight failed")


class AutoResearch:
    def __init__(self, project_dir: str | Path):
        from .store import init_db
        from .workspace import resolve_root

        self.dir = resolve_root(project_dir).resolve()
        self._state_dir = self.dir / ".autoexp"
        self._ledger_path = self._state_dir / "research.jsonl"
        self._diff_dir = self._state_dir / "research-diffs"
        self._proc: subprocess.Popen | None = None
        self._mu = threading.Lock()
        init_db(self.dir)
        if self._contract_ready():
            self._resolve_contract()
            self._migrate_legacy_ledger()

    def _load_config(self):
        try:
            return ResearchConfig.load(self.dir)
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    def _require_config(self):
        config = self._load_config()
        if config is None:
            raise ValueError(f"{PROJECT_CONFIG} contains an invalid autoresearch configuration")
        return config

    def _configured_path(self, path):
        role = self._require_config().role_of(path)
        rel = Path(path)
        target = self.dir / rel
        if (
            role is None
            or rel.is_absolute()
            or ".." in rel.parts
            or target.is_symlink()
            or not target.resolve(strict=False).is_relative_to(self.dir.resolve())
        ):
            raise ValueError(f"unknown or unsafe research file: {path}")
        return target, role

    @staticmethod
    def _full_hash(path):
        return f"sha256:{hashlib.sha256(Path(path).read_bytes()).hexdigest()}"

    @staticmethod
    def _display_hash(value):
        digest = value.removeprefix("sha256:")
        return f"sha256:{digest[:8]}…{digest[-6:]}"

    def preflight(self, *, require_agent=True):
        from .preflight import standard_preflight

        standard = standard_preflight(self.dir, self.dir)
        checks = list(standard["checks"])

        def add(name, ok, detail="", required=True):
            checks.append({
                "name": name,
                "ok": bool(ok),
                "detail": str(detail),
                "required": bool(required),
            })

        config = self._load_config()
        add(
            "research_config",
            config is not None,
            f"{PROJECT_CONFIG} contains an invalid autoresearch configuration",
        )
        evaluator_hash = None
        if config:
            objective = config.objective
            add("objective", bool(objective.metric.strip()), "research objective metric is required")
            add("direction", objective.direction in {"min", "max"}, "research direction must be min or max")
            add(
                "budget",
                isinstance(objective.budget_sec, int) and objective.budget_sec > 0,
                "research budget_sec must be a positive integer",
            )
            add(
                "baseline",
                objective.baseline is None
                or (
                    isinstance(objective.baseline, (int, float))
                    and math.isfinite(objective.baseline)
                ),
                "research baseline must be a finite number or null",
            )
            for role in ("human", "agent", "frozen"):
                matches = [item for item in config.files if item.role == role]
                add(f"{role}_role", len(matches) == 1, f"research requires exactly one {role} file")
                if len(matches) == 1:
                    try:
                        path, _ = self._configured_path(matches[0].path)
                        add(f"{role}_file", path.is_file(), f"missing research file: {matches[0].path}")
                        if role == "frozen" and path.is_file():
                            evaluator_hash = self._full_hash(path)
                    except (OSError, ValueError) as exc:
                        add(f"{role}_file", False, exc)
            metric_path = Path(config.metric.path)
            metric_safe = (
                bool(metric_path.name)
                and not metric_path.is_absolute()
                and ".." not in metric_path.parts
            )
            add(
                "metric_source",
                config.metric.kind in {"json", "regex"} and metric_safe,
                "invalid metric source",
            )
            if config.metric.kind == "regex":
                try:
                    pattern = re.compile(config.metric.key)
                    add("metric_regex", pattern.groups == 1, "metric regex must contain one capture group")
                except re.error as exc:
                    add("metric_regex", False, exc)
            command_ok = (
                isinstance(config.agent_cmd, list)
                and bool(config.agent_cmd)
                and all(isinstance(part, str) and part for part in config.agent_cmd)
            )
            add("agent_command", command_ok, "configured research agent command is invalid", require_agent)
            executable = shutil.which(config.agent_cmd[0]) if command_ok else None
            add(
                "agent_executable",
                bool(executable),
                f"research agent command not found: {config.agent_cmd[0] if command_ok else ''}",
                require_agent,
            )
        ok = all(item["ok"] or not item["required"] for item in checks)
        return {"ok": ok, "checks": checks, "evaluator_hash": evaluator_hash}

    def _contract_ready(self):
        result = self.preflight(require_agent=False)
        return result["ok"] and bool(result.get("evaluator_hash"))

    def _contract_values(self):
        from .runner import hash_json

        config = self._require_config()
        evaluator_hash = self._full_hash(self._configured_path(config.evaluator_path)[0])
        metric_source = {
            "kind": config.metric.kind,
            "path": config.metric.path,
            "key": config.metric.key,
        }
        identity = {
            "metric": config.objective.metric,
            "direction": config.objective.direction,
            "baseline": config.objective.baseline,
            "budget_sec": config.objective.budget_sec,
            "evaluator_path": config.evaluator_path,
            "evaluator_hash": evaluator_hash,
            "program_path": config.program_path,
            "subject_path": config.subject_path,
            "metric_source": metric_source,
        }
        return identity | {"contract_hash": hash_json(identity)}

    @staticmethod
    def _decode_contract(row):
        if not row:
            return None
        value = dict(row)
        value["metric_source"] = json.loads(value["metric_source"])
        value["agent_command"] = json.loads(value["agent_command"])
        return value

    def _contract_row(self, contract_id):
        from .store import db

        conn = db(self.dir)
        row = conn.execute(
            "select * from research_contracts where contract_id = ?",
            (contract_id,),
        ).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"unknown research contract: {contract_id}")
        return row

    def _active_contract(self):
        from .store import db

        conn = db(self.dir)
        row = conn.execute(
            "select * from research_contracts where status = 'active' order by rowid desc limit 1"
        ).fetchone()
        conn.close()
        return self._decode_contract(row)

    def _resolve_contract(self):
        from .provenance import create_trigger
        from .snapshots import capture_workspace
        from .store import db
        from .workspace import now, project_id

        values = self._contract_values()
        current = self._active_contract()
        if current and current["contract_hash"] == values["contract_hash"]:
            return current
        session = self._session()
        if session and current and session["contract_id"] == current["contract_id"]:
            self._update_session(
                session["session_id"],
                status="interrupted",
                ended_at=now(),
                failure_message="research contract changed while the agent was active",
            )
        trigger = create_trigger(
            "autoresearch",
            root=self.dir,
            actor_name="autoexp-autoresearch",
            metadata={"operation": "research_contract"},
        )
        snapshot = capture_workspace(
            self.dir,
            parent_snapshot_id=current.get("best_snapshot_id") if current else None,
            created_by_trigger_id=trigger["trigger_id"],
            label="Research contract boundary",
        )
        timestamp = now()
        contract_id = f"research_{values['contract_hash'][:8]}_{uuid.uuid4().hex[:6]}"
        config = self._require_config()
        conn = db(self.dir)
        try:
            conn.execute("begin immediate")
            if current:
                conn.execute(
                    """update research_contracts set status = 'superseded', ended_at = ?
                       where contract_id = ? and status = 'active'""",
                    (timestamp, current["contract_id"]),
                )
            conn.execute(
                """insert into research_contracts(
                   contract_id, project_id, parent_contract_id, status, contract_hash,
                   metric, direction, baseline_score, best_score, best_snapshot_id,
                   evaluator_path, evaluator_hash, program_path, subject_path,
                   budget_sec, metric_source, agent_command, created_at, ended_at
               ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)""",
                (
                    contract_id,
                    project_id(self.dir),
                    current["contract_id"] if current else None,
                    values["contract_hash"],
                    values["metric"],
                    values["direction"],
                    values["baseline"],
                    values["baseline"],
                    snapshot["snapshot_id"],
                    values["evaluator_path"],
                    values["evaluator_hash"],
                    values["program_path"],
                    values["subject_path"],
                    values["budget_sec"],
                    json.dumps(values["metric_source"], sort_keys=True),
                    json.dumps(config.agent_cmd),
                    timestamp,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
        finally:
            conn.close()
        active = self._active_contract()
        if not active or active["contract_hash"] != values["contract_hash"]:
            raise ValueError("research contract changed concurrently; retry")
        return active

    def _migrate_legacy_ledger(self):
        from .runs import get_run
        from .store import db
        from .workspace import now, write_json

        conn = db(self.dir)
        complete = conn.execute(
            "select research_migration_complete from schema_metadata"
        ).fetchone()[0]
        conn.close()
        if complete:
            return
        diagnostics = []
        legacy_rows = []
        if self._ledger_path.is_file():
            for line_number, line in enumerate(self._ledger_path.read_text().splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict) or not re.fullmatch(
                        r"a\d+", str(value.get("tag", ""))
                    ):
                        raise ValueError("missing valid attempt tag")
                    legacy_rows.append((line_number, value))
                except (ValueError, json.JSONDecodeError) as exc:
                    diagnostics.append({"line": line_number, "error": str(exc)})
        contract = self._active_contract()
        conn = db(self.dir)
        conn.execute("begin immediate")
        seen = set()
        scored_rows = []
        kept_rows = []
        for line_number, legacy in legacy_rows:
            attempt_id = legacy["tag"]
            if attempt_id in seen:
                diagnostics.append({"line": line_number, "error": f"duplicate attempt tag: {attempt_id}"})
                continue
            seen.add(attempt_id)
            run = None
            if legacy.get("run_id"):
                try:
                    run = get_run(legacy["run_id"], self.dir)
                except ValueError:
                    diagnostics.append({"line": line_number, "error": "referenced run is missing"})
            base = (
                conn.execute(
                    "select snapshot_id from source_snapshots where git_commit = ? order by rowid limit 1",
                    (legacy.get("base_commit"),),
                ).fetchone()
                if legacy.get("base_commit")
                else None
            )
            old_status = legacy.get("status")
            score = legacy.get("score")
            scored = (
                old_status in {"kept", "reverted"}
                and isinstance(score, (int, float))
                and math.isfinite(score)
            )
            status = "scored" if scored else "failed"
            verdict = old_status if scored else None
            failure = None if scored else f"legacy attempt imported from {old_status or 'unknown'} state"
            candidate_id = run.get("source_snapshot_id") if run else None
            conn.execute(
                """insert or ignore into research_attempts(
                       contract_id, attempt_id, sequence, session_id, status, hypothesis,
                       base_snapshot_id, candidate_snapshot_id, run_id, score, verdict,
                       best_score_before, improvement, created_at, ended_at,
                       failure_message, metadata
                   ) values (?, ?, ?, null, ?, ?, ?, ?, ?, ?, ?, null, null, ?, ?, ?, ?)""",
                (
                    contract["contract_id"],
                    attempt_id,
                    int(attempt_id[1:]),
                    status,
                    str(legacy.get("hyp") or "Legacy attempt"),
                    base[0] if base else None,
                    candidate_id,
                    run.get("run_id") if run else None,
                    float(score) if scored else None,
                    verdict,
                    run.get("created_at") if run else None,
                    run.get("ended_at") if run else now(),
                    failure,
                    json.dumps({"legacy_line": line_number, "legacy": legacy}, sort_keys=True),
                ),
            )
            if scored:
                scored_rows.append((float(score), candidate_id))
                if verdict == "kept":
                    kept_rows.append((float(score), candidate_id))
        if scored_rows:
            baseline = scored_rows[0][0]
            choose = min if contract["direction"] == "min" else max
            best = choose(kept_rows, key=lambda item: item[0]) if kept_rows else None
            conn.execute(
                """update research_contracts
                   set baseline_score = coalesce(baseline_score, ?),
                       best_score = coalesce(?, best_score),
                       best_snapshot_id = coalesce(?, best_snapshot_id)
                   where contract_id = ?""",
                (
                    baseline,
                    best[0] if best else None,
                    best[1] if best else None,
                    contract["contract_id"],
                ),
            )
        conn.execute("update schema_metadata set research_migration_complete = 1")
        conn.commit()
        conn.close()
        write_json(
            self._state_dir / "research-migration.json",
            {
                "source": self._ledger_path.relative_to(self.dir).as_posix(),
                "imported": len(seen),
                "diagnostics": diagnostics,
            },
        )

    @staticmethod
    def _decode_attempt(row):
        value = dict(row)
        value["metadata"] = json.loads(value.get("metadata") or "{}")
        value["key"] = f"{value['contract_id']}:{value['attempt_id']}"
        value["tag"] = value["attempt_id"]
        value["hyp"] = value["hypothesis"]
        value["state"] = value["status"]
        value["status"] = value["verdict"] or value["status"]
        value["has_diff"] = bool(
            value.get("base_snapshot_id")
            and value.get("candidate_snapshot_id")
            and value["base_snapshot_id"] != value["candidate_snapshot_id"]
        )
        return value

    def _attempt(self, key):
        from .store import db

        if ":" in key:
            contract_id, attempt_id = key.split(":", 1)
        else:
            contract_id = self._resolve_contract()["contract_id"]
            attempt_id = key
        conn = db(self.dir)
        row = conn.execute(
            "select * from research_attempts where contract_id = ? and attempt_id = ?",
            (contract_id, attempt_id),
        ).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"unknown research attempt: {key}")
        return self._decode_attempt(row)

    def _experiments(self):
        from .store import db

        conn = db(self.dir)
        rows = conn.execute("select * from research_attempts order by rowid desc").fetchall()
        conn.close()
        return [self._decode_attempt(row) for row in rows]

    def _session(self):
        from .store import db

        conn = db(self.dir)
        row = conn.execute(
            "select * from research_sessions where status = 'running' order by rowid desc limit 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _latest_session(self):
        from .store import db

        conn = db(self.dir)
        row = conn.execute(
            "select * from research_sessions order by rowid desc limit 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _pid_alive(self, session):
        pid = session.get("pid") if session else None
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            cmdline = Path(f"/proc/{pid}/cmdline")
            if cmdline.is_file():
                expected = Path(self._require_config().agent_cmd[0]).name.encode()
                return expected in cmdline.read_bytes()
            return True
        except (OSError, ValueError):
            return False

    def _update_session(self, session_id, **values):
        from .store import db

        if not session_id:
            return
        conn = db(self.dir)
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(
            f"update research_sessions set {assignments} where session_id = ? and status = 'running'",
            (*values.values(), session_id),
        )
        conn.commit()
        conn.close()

    def _sync_session(self):
        from .workspace import now

        session = self._session()
        if not session:
            return None
        if self._proc and self._proc.pid == session["pid"]:
            returncode = self._proc.poll()
            if returncode is not None:
                self._update_session(
                    session["session_id"],
                    status="completed" if returncode == 0 else "failed",
                    ended_at=now(),
                    failure_message=None
                    if returncode == 0
                    else f"agent exited with status {returncode}",
                )
                self._proc = None
                return None
        if not self._pid_alive(session):
            self._update_session(
                session["session_id"],
                status="interrupted",
                ended_at=now(),
                failure_message="research agent process is no longer running",
            )
            return None
        return self._session()

    def _loop_view(self):
        session = self._sync_session()
        if not session:
            latest = self._latest_session()
            return {
                "active": False,
                "phase": "idle",
                "tag": None,
                "session_id": latest.get("session_id") if latest else None,
                "status": latest.get("status") if latest else "idle",
                "failure_message": latest.get("failure_message") if latest else None,
            }
        return {
            "active": True,
            "phase": session["phase"],
            "tag": session.get("attempt_id"),
            "session_id": session["session_id"],
            "status": session["status"],
            "failure_message": session.get("failure_message"),
        }

    def _restore_snapshot(self, snapshot_id):
        from .runs import copy_run_source
        from .snapshots import materialize_snapshot

        if not snapshot_id:
            return
        with tempfile.TemporaryDirectory(prefix="autoexp-research-restore-") as tmp:
            materialize_snapshot(snapshot_id, tmp, self.dir)
            copy_run_source(tmp, self.dir)

    def _score_run(self, run_id, contract):
        from .artifacts import artifact_content, list_artifacts

        metric = contract["metric_source"]
        wanted = f"output/{Path(metric['path']).as_posix()}"
        artifact = next(
            (
                item
                for item in list_artifacts(run_id, self.dir, category="output")
                if item["path"] == wanted
            ),
            None,
        )
        if not artifact:
            return None
        _, raw = artifact_content(run_id, artifact["artifact_id"], self.dir)
        try:
            if metric["kind"] == "json":
                value = json.loads(raw)
                for part in filter(None, metric.get("key", "").split(".")):
                    value = value[part]
            else:
                match = re.search(metric["key"], raw.decode(errors="replace"))
                value = match.group(1) if match else None
            score = float(value)
            return score if math.isfinite(score) else None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def begin_attempt(self, hypothesis):
        from .execution import execute
        from .provenance import create_trigger
        from .snapshots import capture_workspace
        from .store import db
        from .workspace import now

        hypothesis = str(hypothesis).strip()
        if not hypothesis:
            raise ValueError("research hypothesis is required")
        result = self.preflight(require_agent=False)
        if not result["ok"]:
            raise ResearchPreflightError(result)
        contract = self._resolve_contract()
        trigger = create_trigger(
            "autoresearch",
            root=self.dir,
            actor_name="autoexp-autoresearch",
            metadata={"operation": "candidate_snapshot"},
        )
        candidate = capture_workspace(
            self.dir,
            parent_snapshot_id=contract["best_snapshot_id"],
            created_by_trigger_id=trigger["trigger_id"],
            label="Research candidate",
        )
        session = self._session()
        conn = db(self.dir)
        try:
            conn.execute("begin immediate")
            sequence = conn.execute(
                "select coalesce(max(sequence), 0) + 1 from research_attempts where contract_id = ?",
                (contract["contract_id"],),
            ).fetchone()[0]
            attempt_id = f"a{sequence:02d}"
            conn.execute(
                """insert into research_attempts(
                       contract_id, attempt_id, sequence, session_id, status, hypothesis,
                       base_snapshot_id, candidate_snapshot_id, run_id, score, verdict,
                       best_score_before, improvement, created_at, ended_at,
                       failure_message, metadata
                   ) values (?, ?, ?, ?, 'running', ?, ?, ?, null, null, null, ?, null, ?, null, null, '{}')""",
                (
                    contract["contract_id"],
                    attempt_id,
                    sequence,
                    session["session_id"] if session else None,
                    hypothesis,
                    contract["best_snapshot_id"],
                    candidate["snapshot_id"],
                    contract["best_score"],
                    now(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("another research attempt is already running") from exc
        finally:
            conn.close()
        self._update_session(
            session["session_id"] if session else None,
            phase="train",
            attempt_id=attempt_id,
        )
        key = f"{contract['contract_id']}:{attempt_id}"
        try:
            run = execute(
                root=self.dir,
                snapshot_id=candidate["snapshot_id"],
                trigger_kind="autoresearch",
                actor_name="autoexp-autoresearch",
                session_id=session["session_id"] if session else None,
                request_id=key,
                metadata={
                    "contract_id": contract["contract_id"],
                    "attempt_id": attempt_id,
                    "title": hypothesis[:80],
                },
                environment={
                    "AUTOEXP_RESEARCH_TAG": attempt_id,
                    "AUTOEXP_RESEARCH_BUDGET_SEC": str(contract["budget_sec"]),
                },
                timeout_sec=contract["budget_sec"],
            )
        except Exception as exc:
            self._fail_attempt(key, str(exc))
            raise
        conn = db(self.dir)
        conn.execute(
            """update research_attempts set run_id = ?
               where contract_id = ? and attempt_id = ? and status = 'running'""",
            (run["run_id"], contract["contract_id"], attempt_id),
        )
        conn.commit()
        conn.close()
        if run["status"] != "success":
            self._fail_attempt(
                key,
                run.get("failure_message") or f"run ended as {run['status']}",
            )
        else:
            self._update_session(
                session["session_id"] if session else None,
                phase="score",
                attempt_id=attempt_id,
            )
        attempt = self._attempt(key)
        return {
            "active": False,
            "job": {
                "job_id": attempt_id,
                "run_id": run["run_id"],
                "status": run["status"],
            },
            "attempt": attempt,
        }

    def _fail_attempt(self, key, message):
        from .store import db
        from .workspace import now

        attempt = self._attempt(key)
        if attempt["state"] != "running":
            return attempt
        conn = db(self.dir)
        conn.execute(
            """update research_attempts
               set status = 'failed', ended_at = ?, failure_message = ?
               where contract_id = ? and attempt_id = ? and status = 'running'""",
            (
                now(),
                str(message),
                attempt["contract_id"],
                attempt["attempt_id"],
            ),
        )
        conn.commit()
        conn.close()
        self._restore_snapshot(attempt["base_snapshot_id"])
        self._update_session(attempt.get("session_id"), phase="propose", attempt_id=None)
        return self._attempt(key)

    def finish_attempt(self, key):
        from .runs import TERMINAL_STATUSES, get_run
        from .store import db
        from .workspace import now

        attempt = self._attempt(key)
        if attempt["state"] != "running":
            return attempt
        if not attempt.get("run_id"):
            return self._fail_attempt(key, "attempt did not allocate a run")
        run = get_run(attempt["run_id"], self.dir)
        if run["status"] not in TERMINAL_STATUSES:
            raise ValueError(f"attempt run is not terminal: {run['run_id']}")
        if run["status"] != "success":
            return self._fail_attempt(
                key,
                run.get("failure_message") or f"run ended as {run['status']}",
            )
        contract = self._decode_contract(self._contract_row(attempt["contract_id"]))
        score = self._score_run(run["run_id"], contract)
        if score is None:
            return self._fail_attempt(key, "objective score is missing or invalid")
        best = contract["best_score"]
        improves = best is None or (
            score < best if contract["direction"] == "min" else score > best
        )
        verdict = "kept" if improves else "reverted"
        improvement = (
            0.0
            if best is None
            else best - score
            if contract["direction"] == "min"
            else score - best
        )
        conn = db(self.dir)
        conn.execute("begin immediate")
        conn.execute(
            """update research_attempts
               set status = 'scored', score = ?, verdict = ?, improvement = ?, ended_at = ?
               where contract_id = ? and attempt_id = ? and status = 'running'""",
            (
                score,
                verdict,
                improvement,
                now(),
                attempt["contract_id"],
                attempt["attempt_id"],
            ),
        )
        if improves:
            conn.execute(
                """update research_contracts
                   set baseline_score = coalesce(baseline_score, ?),
                       best_score = ?, best_snapshot_id = ?
                   where contract_id = ? and status = 'active'""",
                (
                    score,
                    score,
                    attempt["candidate_snapshot_id"],
                    attempt["contract_id"],
                ),
            )
        conn.commit()
        conn.close()
        if not improves:
            self._restore_snapshot(attempt["base_snapshot_id"])
        self._update_session(attempt.get("session_id"), phase="propose", attempt_id=None)
        return self._attempt(key)

    def start_loop(self):
        from .store import db
        from .workspace import now

        result = self.preflight(require_agent=True)
        if not result["ok"]:
            raise ResearchPreflightError(result)
        with self._mu:
            active = self._sync_session()
            if active:
                return {"active": True, "job": active}
            contract = self._resolve_contract()
            log_path = self._state_dir / "research-agent.log"
            session_id = f"research_session_{uuid.uuid4().hex}"
            conn = db(self.dir)
            try:
                conn.execute("begin immediate")
                existing = conn.execute(
                    """select * from research_sessions
                       where contract_id = ? and status = 'running' limit 1""",
                    (contract["contract_id"],),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return {"active": True, "job": dict(existing)}
                with log_path.open("a") as log:
                    proc = subprocess.Popen(
                        self._require_config().agent_cmd,
                        cwd=self.dir,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                conn.execute(
                    """insert into research_sessions(
                       session_id, contract_id, status, phase, attempt_id, pid,
                       log_path, started_at, ended_at, failure_message
                   ) values (?, ?, 'running', 'propose', null, ?, ?, ?, null, null)""",
                    (
                        session_id,
                        contract["contract_id"],
                        proc.pid,
                        log_path.relative_to(self.dir).as_posix(),
                        now(),
                    ),
                )
                conn.commit()
            except OSError as exc:
                conn.rollback()
                raise ValueError(f"could not start research agent: {exc}") from exc
            finally:
                conn.close()
            self._proc = proc
            return {"active": True, "job": self._session()}

    def stop_loop(self):
        from .workspace import now

        with self._mu:
            session = self._sync_session()
            if not session:
                return {"active": False, "job": None}
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(session["pid"]), signal.SIGTERM)
                else:
                    os.kill(session["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._update_session(session["session_id"], status="stopped", ended_at=now())
            self._proc = None
            return {"active": False, "job": None}

    def log(self, tail_bytes=65536):
        path = self._state_dir / "research-agent.log"
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            return handle.read().decode(errors="replace")

    def state(self):
        config = self._load_config()
        preflight = self.preflight(require_agent=True)
        contract = self._resolve_contract() if self._contract_ready() else self._active_contract()
        files = []
        if config:
            for item in config.files:
                entry = {"path": item.path, "role": item.role, "desc": item.desc}
                try:
                    path, _ = self._configured_path(item.path)
                    if item.role == "frozen" and path.is_file():
                        entry["hash"] = self._display_hash(self._full_hash(path))
                except ValueError:
                    pass
                files.append(entry)
        objective = {
            "metric": contract["metric"] if contract else (config.objective.metric if config else "score"),
            "direction": contract["direction"] if contract else (config.objective.direction if config else "max"),
            "baseline": contract["baseline_score"] if contract else None,
            "best": contract["best_score"] if contract else None,
            "budget_sec": contract["budget_sec"] if contract else (config.objective.budget_sec if config else 0),
            "current_best_snapshot_id": contract["best_snapshot_id"] if contract else None,
        }
        public_contract = (
            contract | {"current_best_snapshot_id": contract["best_snapshot_id"]}
            if contract
            else {
                "contract_id": "unavailable",
                "status": "blocked",
                "current_best_snapshot_id": None,
                "evaluator_hash": preflight.get("evaluator_hash"),
            }
        )
        return {
            "contract": public_contract,
            "objective": objective,
            "files": files,
            "experiments": self._experiments(),
            "loop": self._loop_view(),
            "preflight": preflight,
            "can_import_baseline": self.can_import_baseline(),
        }

    def diff(self, key):
        from .snapshots import diff_snapshots

        attempt = self._attempt(key)
        if attempt.get("base_snapshot_id") and attempt.get("candidate_snapshot_id"):
            value = diff_snapshots(
                attempt["base_snapshot_id"],
                attempt["candidate_snapshot_id"],
                self.dir,
            )
        else:
            legacy = self._diff_dir / f"{attempt['attempt_id']}.diff"
            value = legacy.read_text() if legacy.is_file() else ""
        return {"attempt": attempt, "tag": attempt["attempt_id"], "diff": value}

    def open_file(self, path):
        target, role = self._configured_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"missing research file: {path}")
        entry = {"path": path, "text": target.read_text(), "role": role}
        if role == "frozen":
            entry["hash"] = self._display_hash(self._full_hash(target))
        return entry

    def _ensure_no_running_attempt(self):
        from .store import db

        conn = db(self.dir)
        running = conn.execute(
            "select 1 from research_attempts where status = 'running' limit 1"
        ).fetchone()
        conn.close()
        if running:
            raise ValueError("research files cannot be edited while an attempt is running")

    def save_file(self, path, text):
        from .provenance import create_trigger
        from .snapshots import capture_workspace
        from .store import db

        if not isinstance(text, str):
            raise ValueError("text must be a string")
        target, role = self._configured_path(path)
        if role == "frozen":
            raise ValueError("the evaluator is frozen for the active research contract")
        self._ensure_no_running_attempt()
        contract = self._resolve_contract()
        target.write_text(text)
        trigger = create_trigger(
            "human",
            root=self.dir,
            actor_name="autoexp-view",
            metadata={"operation": "research_file_edit", "path": path},
        )
        snapshot = capture_workspace(
            self.dir,
            parent_snapshot_id=contract["best_snapshot_id"],
            created_by_trigger_id=trigger["trigger_id"],
            label=f"Edited {path}",
        )
        if role == "human":
            conn = db(self.dir)
            conn.execute(
                """update research_contracts set best_snapshot_id = ?
                   where contract_id = ? and status = 'active'""",
                (snapshot["snapshot_id"], contract["contract_id"]),
            )
            conn.commit()
            conn.close()
        return self.open_file(path) | {"snapshot": snapshot}

    def save_program(self, text):
        return self.save_file(self._require_config().program_path, text)

    def can_import_baseline(self):
        from .store import db

        config = self._load_config()
        if not config:
            return False
        conn = db(self.dir)
        count = conn.execute("select count(*) from research_attempts").fetchone()[0]
        conn.close()
        try:
            return (
                count == 0
                and self._configured_path(config.subject_path)[0].read_text() == TRAIN_TEXT
            )
        except OSError:
            return False

    def save_subject(self, text):
        from .store import db

        if not self.can_import_baseline():
            raise ValueError(
                "baseline import is only available before attempts and before candidate.py is edited"
            )
        result = self.save_file(self._require_config().subject_path, text)
        contract = self._resolve_contract()
        conn = db(self.dir)
        conn.execute(
            """update research_contracts set best_snapshot_id = ?
               where contract_id = ? and status = 'active'""",
            (result["snapshot"]["snapshot_id"], contract["contract_id"]),
        )
        conn.commit()
        conn.close()
        return result


def ensure_research_file_editable(root, script_path):
    """Reject frozen evaluator edits through generic HTTP/MCP script surfaces."""
    try:
        config = ResearchConfig.load(Path(root))
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return
    project_path = f"{EXPERIMENT_DIR}/{Path(script_path).as_posix()}"
    if config.role_of(project_path) == "frozen":
        raise ValueError("the evaluator is frozen for the active research contract")


PROGRAM_TEXT = """# Autoresearch program

Improve the objective by editing only `experiment/candidate.py`.

If you have an existing training or experiment script, use it as the starting
point for `experiment/candidate.py` before beginning the first attempt.

The loop:

1. Read the current research state with the `research_state` MCP tool.
2. Form one concrete hypothesis and edit `experiment/candidate.py`.
3. Call `research_begin_attempt` with the hypothesis. It runs the experiment.
4. Call `research_finish_attempt` with the returned attempt tag.
5. Study kept and reverted attempts, then repeat until stopped.

Mark only a new best, surprising failure, or decision-changing result with
`mark_milestone`. Before concluding, read `project_summary` and write one
end-to-end synthesis with `write_project_report`.

`experiment/evaluate.py` is the fixed evaluator. Do not edit it.
The experiment budget is available as `AUTOEXP_RESEARCH_BUDGET_SEC`.
Keep each attempt focused so its diff and result remain understandable.
"""

TRAIN_TEXT = """import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
ctx = json.loads(Path(parser.parse_args().ctx).read_text())

# This is the agent-owned subject. Replace the simple candidate with the
# implementation or artifact your research project is optimizing.
candidate = {"score": 0.5, "attempt": os.environ.get("AUTOEXP_RESEARCH_TAG", "baseline")}
output = Path(ctx["output_dir"])
output.mkdir(parents=True, exist_ok=True)
(output / "candidate.json").write_text(json.dumps(candidate, indent=2) + "\\n")
"""

EVALUATE_TEXT = """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ctx", required=True)
ctx = json.loads(Path(parser.parse_args().ctx).read_text())
output = Path(ctx["output_dir"])
candidate = json.loads((output / "candidate.json").read_text())

# Replace this with the stable evaluator for your domain.
metrics = {"score": float(candidate["score"])}
(output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\\n")
print(f"score: {metrics['score']}")
"""


def config_block():
    return {
        "mode": "autoresearch",
        "source": {"root": EXPERIMENT_DIR, "editable": ["candidate.py"]},
        "autoresearch": {
            "objective": {
                "metric": "score",
                "direction": "max",
                "baseline": None,
                "budget_sec": 300,
            },
            "files": [
                {
                    "path": "experiment/program.md",
                    "role": "human",
                    "desc": "research directions and loop rules",
                },
                {
                    "path": "experiment/candidate.py",
                    "role": "agent",
                    "desc": "the implementation the agent improves",
                },
                {
                    "path": "experiment/evaluate.py",
                    "role": "frozen",
                    "desc": "the fixed evaluator",
                },
            ],
            "metric": {"kind": "json", "path": "metrics.json", "key": "score"},
            "agent": {
                "cmd": [
                    "codex",
                    "exec",
                    "Read experiment/program.md and run the autoresearch loop using the Autoexp MCP tools.",
                ],
            },
        },
    }


def scaffold(root, write_json):
    script = root / EXPERIMENT_DIR
    (script / "program.md").write_text(PROGRAM_TEXT)
    (script / "candidate.py").write_text(TRAIN_TEXT)
    (script / "evaluate.py").write_text(EVALUATE_TEXT)
    write_json(
        root / STAGE_MANIFEST,
        {
            "name": "candidate.py",
            "command": "python candidate.py --ctx ${CTX} && python evaluate.py --ctx ${CTX}",
            "working_dir": EXPERIMENT_DIR,
            "interface_version": "1",
        },
    )
    write_json(root / PARAMS_FILE, {})
    write_json(root / PARAMS_SCHEMA_FILE, {"type": "object", "properties": {}})


def for_project(root):
    return AutoResearch(root)
