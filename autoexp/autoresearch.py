"""Durable Autoresearch contracts and attempts on Autoexp's shared execution plane."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .workspace import (
    PROJECT_CONFIG,
    experiment_id,
    repository_root,
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
        from .workspace import experiment_config
        config = experiment_config(project_dir)
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
        self.experiment_id = experiment_id(self.dir)
        self._state_dir = self.dir
        self._proc = None
        self._mu = threading.Lock()
        init_db(self.dir)
        if self._contract_ready():
            self._resolve_contract()

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
        from .workspace import repository_root
        target = repository_root(self.dir) / rel
        if (
            role is None
            or rel.is_absolute()
            or ".." in rel.parts
            or target.is_symlink()
            or not target.resolve(strict=False).is_relative_to(repository_root(self.dir).resolve())
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

        from .workspace import materialize_workspace
        with tempfile.TemporaryDirectory(prefix="autoexp-research-preflight-") as tmp:
            materialize_workspace(self.dir, tmp)
            standard = standard_preflight(self.dir, tmp)
        checks = list(standard["checks"])

        def add(name, ok, detail="", required=True):
            checks.append({
                "name": name,
                "ok": bool(ok),
                "detail": "" if ok else str(detail),
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
            "select * from research_contracts where experiment_id = ? and status = 'active' order by rowid desc limit 1",
            (self.experiment_id,),
        ).fetchone()
        conn.close()
        return self._decode_contract(row)

    def _resolve_contract(self):
        from .provenance import create_trigger
        from .snapshots import capture_workspace
        from .store import db
        from .workspace import experiment_id, now

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
                   contract_id, experiment_id, parent_contract_id, status, contract_hash,
                   metric, direction, baseline_score, best_score, best_snapshot_id,
                   evaluator_path, evaluator_hash, program_path, subject_path,
                   budget_sec, metric_source, agent_command, created_at, ended_at
               ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)""",
                (
                    contract_id,
                    self.experiment_id,
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
        rows = conn.execute("select a.* from research_attempts a join research_contracts c on c.contract_id = a.contract_id where c.experiment_id = ? order by a.rowid desc", (self.experiment_id,)).fetchall()
        conn.close()
        return [self._decode_attempt(row) for row in rows]

    def _session(self):
        from .store import db

        conn = db(self.dir)
        row = conn.execute(
            """select s.* from research_sessions s join research_contracts c on c.contract_id = s.contract_id
               where c.experiment_id = ? and s.status = 'running' order by s.rowid desc limit 1""",
            (self.experiment_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _latest_session(self):
        from .store import db

        conn = db(self.dir)
        row = conn.execute(
            """select s.* from research_sessions s join research_contracts c on c.contract_id = s.contract_id
               where c.experiment_id = ? order by s.rowid desc limit 1""",
            (self.experiment_id,),
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

    def state(self):
        config = self._load_config()
        preflight = self.preflight(require_agent=True)
        contract = self._active_contract()
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
        }

    def diff(self, key):
        from .snapshots import diff_snapshots

        attempt = self._attempt(key)
        if not attempt.get("base_snapshot_id") or not attempt.get("candidate_snapshot_id"):
            raise ValueError("research attempt has no immutable source snapshots")
        value = diff_snapshots(
            attempt["base_snapshot_id"],
            attempt["candidate_snapshot_id"],
            self.dir,
        )
        return {"attempt": attempt, "tag": attempt["attempt_id"], "diff": value}


def for_project(root):
    return AutoResearch(root)
