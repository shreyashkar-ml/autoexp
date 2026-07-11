"""Autoresearch's keep-or-revert ratchet on top of Autoexp runs and Git."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# ======================================================================
#  Config models  (read from the project's autoexp.json autoresearch block)
# ======================================================================
Role = Literal["human", "agent", "frozen"]
Direction = Literal["min", "max"]


@dataclass
class Objective:
    metric: str                      # e.g. "val_bpb"
    direction: Direction = "min"     # lower-is-better by default
    baseline: float | None = None    # recorded after the first run
    budget_sec: int = 300            # wall-clock per experiment


@dataclass
class FileRole:
    path: str
    role: Role
    desc: str = ""


@dataclass
class MetricSource:
    """Where to read the scalar objective after a run finishes."""
    kind: Literal["json", "regex"] = "json"
    path: str = "metrics.json"       # relative to the run output dir
    key: str = ""                    # dotted json key, or regex w/ one group


@dataclass
class ResearchConfig:
    objective: Objective
    files: list[FileRole]
    metric: MetricSource
    agent_cmd: list[str]

    @classmethod
    def load(cls, project_dir: Path) -> "ResearchConfig":
        """Parse the `autoresearch` block of <project>/autoexp.json."""
        cfg = json.loads((project_dir / "autoexp.json").read_text())
        ar = cfg["autoresearch"]
        return cls(
            objective=Objective(**ar["objective"]),
            files=[FileRole(**f) for f in ar["files"]],
            metric=MetricSource(**ar.get("metric", {})),
            agent_cmd=ar.get("agent", {}).get("cmd", ["codex"]),
        )

    # convenience lookups -------------------------------------------------
    def role_of(self, path: str) -> Role | None:
        return next((f.role for f in self.files if f.path == path), None)

    @property
    def subject_path(self) -> str:
        return next(f.path for f in self.files if f.role == "agent")

    @property
    def program_path(self) -> str:
        return next(f.path for f in self.files if f.role == "human")


# ======================================================================
#  The ratchet
# ======================================================================
class AutoResearch:
    def __init__(self, project_dir: str | Path):
        self.dir = Path(project_dir)
        self.cfg = ResearchConfig.load(self.dir)

        self._state_dir = self.dir / ".autoexp"
        self._state_dir.mkdir(exist_ok=True)
        self._ledger_path = self._state_dir / "research.jsonl"
        self._diff_dir = self._state_dir / "research-diffs"

        self._loop = {"active": False, "phase": "idle", "tag": None}
        self._proc: subprocess.Popen | None = None
        self._mu = threading.Lock()

    # ------------------------------------------------------------------
    #  The evaluator hash is informational; file ownership is a good-faith
    #  contract with the agent, just like the original autoresearch loop.
    # ------------------------------------------------------------------
    def _hash(self, rel_path: str) -> str:
        h = hashlib.sha256((self.dir / rel_path).read_bytes()).hexdigest()
        return f"sha256:{h[:4]}\u2026{h[-2:]}"  # short, display-friendly

    # ------------------------------------------------------------------
    #  Ledger: one JSONL row per attempt (kept AND reverted are retained).
    # ------------------------------------------------------------------
    def _read_ledger(self) -> list[dict]:
        if not self._ledger_path.exists():
            return []
        return [json.loads(l) for l in self._ledger_path.read_text().splitlines() if l.strip()]

    def _rewrite_ledger(self, rows: list[dict]) -> None:
        self._ledger_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _append_ledger(self, row: dict) -> None:
        with self._ledger_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def _update_ledger(self, tag: str, **changes) -> dict:
        rows = self._read_ledger()
        for r in rows:
            if r["tag"] == tag:
                r.update(changes)
                self._rewrite_ledger(rows)
                return r
        raise KeyError(f"no ledger row for {tag}")

    def _next_tag(self) -> str:
        return f"a{len(self._read_ledger()) + 1:02d}"

    def _store_diff(self, tag: str, diff: str) -> bool:
        if not diff:
            return False
        self._diff_dir.mkdir(exist_ok=True)
        (self._diff_dir / f"{tag}.diff").write_text(diff)
        return True

    # ------------------------------------------------------------------
    #  Scoring + the keep-or-revert decision.
    # ------------------------------------------------------------------
    def _baseline(self, rows: list[dict]) -> float | None:
        if self.cfg.objective.baseline is not None:
            return self.cfg.objective.baseline
        first = next(
            (row for row in rows
             if row["status"] == "kept" and row.get("score") is not None),
            None,
        )
        return first["score"] if first else None

    def _best(self, rows: list[dict]) -> float | None:
        kept = [r["score"] for r in rows
                if r["status"] == "kept" and r.get("score") is not None]
        if not kept:
            return self._baseline(rows)
        return min(kept) if self.cfg.objective.direction == "min" else max(kept)

    def _score_run(self, run_id: str) -> float | None:
        """Read the single scalar objective out of a finished run's output."""
        from .runs import get_run
        from .workspace import run_dir_for

        out = run_dir_for(get_run(run_id, self.dir), self.dir) / "output"
        ms = self.cfg.metric
        try:
            if ms.kind == "json":
                data = json.loads((out / ms.path).read_text())
                for part in ms.key.split("."):       # dotted key support
                    data = data[part]
                return float(data)
            if ms.kind == "regex":
                text = (out / ms.path).read_text()
                m = re.search(ms.key, text)
                return float(m.group(1)) if m else None
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        raise ValueError(f"unknown metric source: {ms.kind}")

    def _improves(self, score: float, best: float | None) -> bool:
        if best is None:
            return True  # first scored attempt establishes the baseline
        return score < best if self.cfg.objective.direction == "min" else score > best

    # ------------------------------------------------------------------
    #  Attempt lifecycle (called by the agent or a run-finished hook).
    # ------------------------------------------------------------------
    def _run(self, tag: str) -> dict:
        from .runs import get_run
        from .store import current_autoexp_commit

        base_commit = current_autoexp_commit(self.dir)
        env = os.environ | {
            "AUTOEXP_RESEARCH_TAG": tag,
            "AUTOEXP_RESEARCH_BUDGET_SEC": str(self.cfg.objective.budget_sec),
        }
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "autoexp",
                "run",
                "--trigger-kind",
                "autoresearch",
                "--actor-name",
                "autoexp-autoresearch",
            ],
            cwd=self.dir, env=env, capture_output=True, text=True,
        )
        match = re.search(r"^run_id: (.+)$", proc.stdout, re.MULTILINE)
        if not match:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "run did not produce a run_id")
        run_id = match.group(1)
        return {
            "active": False,
            "job": {
                "job_id": tag,
                "run_id": run_id,
                "status": get_run(run_id, self.dir)["status"],
                "base_commit": base_commit,
            },
        }

    def begin_attempt(self, hyp: str) -> dict:
        """Open a running ledger row and launch one budgeted experiment."""
        tag = self._next_tag()
        self._append_ledger({
            "tag": tag, "status": "running", "score": None,
            "hyp": hyp, "commit": None,
        })
        with self._mu:
            self._loop.update(phase="train", tag=tag)
        job = self._run(tag)
        self._update_ledger(
            tag,
            run_id=job["job"]["run_id"],
            base_commit=job["job"]["base_commit"],
        )
        return job

    def finish_attempt(self, tag: str) -> dict:
        """Score an attempt and keep its commit only when it improves."""
        from .runs import get_run, run_stage_commit
        from .store import autoexp_git

        rows = self._read_ledger()
        row = next(r for r in rows if r["tag"] == tag)
        run_id = row["run_id"]
        score = self._score_run(run_id)
        commit = run_stage_commit(get_run(run_id, self.dir))
        diff = autoexp_git(
            ["diff", row["base_commit"], commit, "--", self.cfg.subject_path],
            root=self.dir, capture=True, check=False,
        )
        changes = {"score": score, "has_diff": self._store_diff(tag, diff)}
        if score is not None and self._improves(score, self._best(rows)):
            autoexp_git(["branch", "-f", f"autoexp/{tag}", commit], root=self.dir)
            changes.update(status="kept", commit=autoexp_git(
                ["rev-parse", "--short", commit], root=self.dir, capture=True,
            ))
        else:
            autoexp_git(["reset", "--hard", row["base_commit"]], root=self.dir)
            changes["status"] = "reverted"
        verdict = self._update_ledger(tag, **changes)
        with self._mu:
            self._loop.update(phase="propose", tag=None)
        return verdict

    # ------------------------------------------------------------------
    #  Loop lifecycle: spawn / stop the coding agent in the repo.
    #  (autoresearch has no orchestrator -- the agent drives the loop;
    #   autoexp just launches it and tracks liveness.)
    # ------------------------------------------------------------------
    def start_loop(self) -> dict:
        with self._mu:
            if self._proc and self._proc.poll() is None:
                return self._job_view()
            # The agent reads program.md and uses the autoexp MCP/CLI to
            # run begin_attempt / finish_attempt in a loop.
            log_path = self._state_dir / "research-agent.log"
            with log_path.open("a") as log:
                self._proc = subprocess.Popen(
                    self.cfg.agent_cmd, cwd=self.dir, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            self._loop.update(active=True, phase="propose")
        return self._job_view()

    def stop_loop(self) -> dict:
        with self._mu:
            if self._proc and self._proc.poll() is None:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                else:
                    self._proc.terminate()
            self._proc = None
            self._loop.update(active=False, phase="idle", tag=None)
        return {"active": False, "job": None}

    def _job_view(self) -> dict:
        if self._proc and self._proc.poll() is not None:
            self._proc = None
            self._loop.update(active=False, phase="idle", tag=None)
        return {"active": self._loop["active"],
                "job": {"job_id": "loop", "status": "running"} if self._loop["active"] else None}

    def log(self, tail_bytes: int = 65536) -> str:
        path = self._state_dir / "research-agent.log"
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            return handle.read().decode(errors="replace")

    # ------------------------------------------------------------------
    #  Endpoint handlers -- shapes match the UI's api.* calls exactly.
    # ------------------------------------------------------------------
    def state(self) -> dict:
        """GET /api/research"""
        files = []
        for f in self.cfg.files:
            entry = {"path": f.path, "role": f.role, "desc": f.desc}
            if f.role == "frozen":
                entry["hash"] = self._hash(f.path)
            files.append(entry)

        rows = self._read_ledger()
        running = next((row for row in reversed(rows) if row["status"] == "running"), None)
        if running:
            self._loop.update(phase="train", tag=running["tag"])
        elif self._loop["active"]:
            self._loop.update(phase="propose", tag=None)
        self._job_view()

        experiments = list(reversed(rows))  # newest first
        for e in experiments:               # trim internal fields
            e.pop("base_commit", None)
            e.pop("diff", None)

        obj = self.cfg.objective
        return {
            "objective": {
                "metric": obj.metric, "direction": obj.direction,
                "baseline": self._baseline(rows), "best": self._best(rows),
                "budget_sec": obj.budget_sec,
            },
            "files": files,
            "experiments": experiments,
            "loop": dict(self._loop),
            "can_import_baseline": self.can_import_baseline(),
        }

    def diff(self, tag: str) -> dict:
        """Read one attempt diff without loading every diff into research state."""
        row = next(r for r in self._read_ledger() if r["tag"] == tag)
        path = self._diff_dir / f"{tag}.diff"
        return {"tag": tag, "diff": path.read_text() if row.get("has_diff") and path.is_file() else ""}

    def open_file(self, path: str) -> dict:
        """GET /api/research/file?path=..."""
        role = self.cfg.role_of(path)
        entry = {"path": path, "text": (self.dir / path).read_text(), "role": role}
        if role == "frozen":
            entry["hash"] = self._hash(path)
        return entry

    def save_program(self, text: str) -> dict:
        """PUT /api/research/program -- human-owned file only."""
        program = self.cfg.program_path
        (self.dir / program).write_text(text)
        return self.open_file(program)

    def can_import_baseline(self) -> bool:
        """Only a fresh scaffold can be replaced by an uploaded baseline."""
        return not self._read_ledger() and (self.dir / self.cfg.subject_path).read_text() == TRAIN_TEXT

    def save_subject(self, text: str) -> dict:
        """PUT /api/research/subject -- initial baseline import into the agent-owned file."""
        if not self.can_import_baseline():
            raise ValueError("baseline import is only available before attempts and before train.py is edited")
        subject = self.cfg.subject_path
        (self.dir / subject).write_text(text)
        return self.open_file(subject)


PROGRAM_TEXT = """# Autoresearch program

Improve the objective by editing only `script/train.py`.

If you have an existing training or experiment script, use it as the starting
point for `script/train.py` before beginning the first attempt.

The loop:

1. Read the current research state with the `research_state` MCP tool.
2. Form one concrete hypothesis and edit `script/train.py`.
3. Call `research_begin_attempt` with the hypothesis. It runs the experiment.
4. Call `research_finish_attempt` with the returned attempt tag.
5. Study kept and reverted attempts, then repeat until stopped.

`script/evaluate.py` is the fixed evaluator. Do not edit it.
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


def config_block() -> dict:
    return {
        "mode": "autoresearch",
        "autoresearch": {
            "objective": {
                "metric": "score",
                "direction": "max",
                "baseline": None,
                "budget_sec": 300,
            },
            "files": [
                {
                    "path": "script/program.md",
                    "role": "human",
                    "desc": "research directions and loop rules",
                },
                {
                    "path": "script/train.py",
                    "role": "agent",
                    "desc": "the implementation the agent improves",
                },
                {
                    "path": "script/evaluate.py",
                    "role": "frozen",
                    "desc": "the fixed evaluator",
                },
            ],
            "metric": {"kind": "json", "path": "metrics.json", "key": "score"},
            "agent": {
                "cmd": [
                    "codex",
                    "exec",
                    "Read script/program.md and run the autoresearch loop using the Autoexp MCP tools.",
                ],
            },
        },
    }


def scaffold(root: Path, write_json) -> None:
    script = root / "script"
    (script / "program.md").write_text(PROGRAM_TEXT)
    (script / "train.py").write_text(TRAIN_TEXT)
    (script / "evaluate.py").write_text(EVALUATE_TEXT)
    write_json(script / "stage.json", {
        "name": "train.py",
        "command": "python train.py --ctx ${CTX} && python evaluate.py --ctx ${CTX}",
        "working_dir": "script",
        "interface_version": "1",
    })
    write_json(script / "params.json", {})
    write_json(script / "params.schema.json", {"type": "object", "properties": {}})


def for_project(root: str | Path) -> AutoResearch:
    return AutoResearch(root)
