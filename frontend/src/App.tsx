import {
  AlertTriangle,
  Check,
  ChevronDown,
  Clipboard,
  Code2,
  FileText,
  FlaskConical,
  FolderOpen,
  GitBranch,
  GitCommit,
  Lock,
  Moon,
  Pencil,
  Play,
  RefreshCw,
  Repeat2,
  Save,
  Search,
  SlidersHorizontal,
  Square,
  Sun,
  Terminal,
  Target,
  Bot,
  Upload,
  X,
  ZoomIn,
  ZoomOut,
  type LucideIcon,
} from "lucide-react";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import hljs from "highlight.js/lib/core";
import ini from "highlight.js/lib/languages/ini";
import jsonLanguage from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";

import { api } from "@/api";
import type {
  Artifact,
  ArtifactDetailPayload,
  ArtifactListPayload,
  InstructionPayload,
  ParamsPayload,
  Project,
  ProjectReportPayload,
  ReportPayload,
  ResearchExperiment,
  ResearchFilePayload,
  ResearchObjective,
  ResearchState,
  Run,
  RunAggregate,
  RunDetailPayload,
  RunDiffPayload,
  RunLogPayload,
  ScriptSavePayload,
  SourcePayload,
} from "@/types";

hljs.registerLanguage("ini", ini);
hljs.registerLanguage("json", jsonLanguage);
hljs.registerLanguage("markdown", markdown);
hljs.registerLanguage("python", python);

type Theme = "light" | "dark";
type ProjectMode = "standard" | "autoresearch";
type StandardTab = "overview" | "script" | "output" | "logs" | "report" | "diff";
type ResearchTab = "diff" | "run" | "artifacts";
type Panel =
  | { kind: "run"; run: Run; tab: StandardTab }
  | { kind: "instruction"; data: InstructionPayload }
  | { kind: "params"; data: ParamsPayload }
  | { kind: "program"; data: ResearchFilePayload }
  | { kind: "evaluator"; data: ResearchFilePayload }
  | { kind: "attempt"; experiment: ResearchExperiment; objective: ResearchObjective }
  | { kind: "preflight"; data: ResearchState }
  | { kind: "project-report"; data: ProjectReportPayload }
  | { kind: "log" };
type JobState = {
  active: boolean;
  job: { status?: string; run_id?: string | null } | null;
  run_id?: string | null;
  run?: Run | null;
};
type Toast = { id: number; message: string; kind?: "ok" | "bad" };
type RunRequestPayload = Run | { run?: Run | null; run_id?: string | null; job?: { run_id?: string | null } };

function statusLabel(status: string) {
  return status === "success" ? "completed" : status || "unknown";
}

function formatScore(score: number | null) {
  if (score === null) return "—";
  const magnitude = Math.abs(score);
  if (magnitude >= 1000) return score.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (magnitude > 0 && magnitude < 0.001) return score.toExponential(2);
  return score.toFixed(4);
}

function attemptKey(attempt: ResearchExperiment) {
  return attempt.key || attempt.attempt_id || attempt.tag || "attempt";
}

function attemptLabel(attempt: ResearchExperiment) {
  return attempt.attempt_id || attempt.tag || attempt.key || "attempt";
}

function attemptHypothesis(attempt: ResearchExperiment) {
  return attempt.hypothesis || attempt.hyp || "No hypothesis recorded";
}

function attemptVerdict(attempt: ResearchExperiment) {
  if (attempt.verdict) return attempt.verdict;
  return attempt.status === "kept" || attempt.status === "reverted" ? attempt.status : null;
}

function attemptState(attempt: ResearchExperiment) {
  return attempt.state || (attempt.status === "failed" ? "failed" : attempt.status === "running" ? "running" : "scored");
}

function relativeTime(value?: string) {
  if (!value) return "";
  const normalized = value.replace(
    /T(\d{2})-(\d{2})-(\d{2})Z$/,
    "T$1:$2:$3Z",
  );
  const seconds = (Date.now() - new Date(normalized).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return value;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function nextScriptPath(path: string, files: { path: string }[]) {
  const parts = path.split("/");
  const name = parts.pop() || "script.py";
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const suffix = dot > 0 ? name.slice(dot) : "";
  const base = stem.replace(/_v\d+$/, "");
  let highest = 1;
  const escaped = base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  for (const file of files) {
    const fileName = file.path.split("/").pop() || "";
    const fileDot = fileName.lastIndexOf(".");
    const fileStem = fileDot > 0 ? fileName.slice(0, fileDot) : fileName;
    const fileSuffix = fileDot > 0 ? fileName.slice(fileDot) : "";
    if (fileSuffix !== suffix) continue;
    const match = fileStem.match(new RegExp(`^${escaped}_v(\\d+)$`));
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  return [...parts, `${base}_v${highest + 1}${suffix}`].filter(Boolean).join("/");
}

function requestedRun(data: RunRequestPayload): Run | null {
  if ("run" in data && data.run?.run_id) return data.run;
  const runId = data.run_id || ("job" in data ? data.job?.run_id : null);
  if (!runId) return null;
  return "status" in data ? data as Run : { run_id: runId, status: "queued" };
}

function normalizeMarkdown(text: string) {
  const lines = text.split("\n");
  const content = lines.filter((line) => line.length > 0);
  return content.length > 0 && content.every((line) => line.startsWith("+"))
    ? lines.map((line) => line.startsWith("+") ? line.slice(1) : line).join("\n")
    : text;
}

export default function App() {
  const [theme, setTheme] = useState<Theme>(() =>
    window.localStorage.getItem("autoexp-theme") === "dark" ? "dark" : "light",
  );
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [projectMode, setProjectMode] = useState<ProjectMode>("standard");
  const [runs, setRuns] = useState<Run[]>([]);
  const [job, setJob] = useState<JobState>({ active: false, job: null });
  const [query, setQuery] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [panel, setPanel] = useState<Panel | null>(null);
  const [leftColumn, setLeftColumn] = useState(52);
  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [research, setResearch] = useState<ResearchState | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const pendingSelection = useRef(false);

  const project = projects.find((item) => item.project_id === projectId) || null;
  const isResearch = project?.mode === "autoresearch";
  const researchView = projectMode === "autoresearch";
  const filteredProjects = projects.filter((item) =>
    projectMode === "autoresearch" ? item.mode === "autoresearch" : item.mode !== "autoresearch",
  );
  const hasAvailableProjects = filteredProjects.some((item) => item.exists);
  const displayedResearch = researchView && project ? research : null;
  const active = isResearch ? Boolean(research?.loop.active) : job.active;
  const researchReady = !isResearch || Boolean(research?.preflight.ok);

  const toast = useCallback((message: string, kind?: Toast["kind"]) => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, message, kind }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((item) => item.id !== id)),
      2800,
    );
  }, []);

  function projectPath(path: string, id = projectId) {
    const joiner = path.includes("?") ? "&" : "?";
    return `${path}${joiner}project_id=${encodeURIComponent(id)}`;
  }

  async function loadProjects(preferred = projectId) {
    const suffix = preferred ? `?project_id=${encodeURIComponent(preferred)}` : "";
    const data = await api<{ projects: Project[]; selected_project_id: string | null }>(
      `/api/projects${suffix}`,
    );
    const items = data.projects || [];
    const candidates = items.filter((item) =>
      projectMode === "autoresearch" ? item.mode === "autoresearch" : item.mode !== "autoresearch",
    );
    const next =
      candidates.find((item) => item.exists && item.project_id === preferred)?.project_id ||
      candidates.find((item) => item.exists && item.project_id === data.selected_project_id)?.project_id ||
      candidates.find((item) => item.exists)?.project_id ||
      "";
    setProjects(items);
    setProjectId(next);
    return next;
  }

  function switchProjectMode(mode: ProjectMode) {
    setProjectMode(mode);
    const next = projects.find((item) =>
      item.exists && (mode === "autoresearch" ? item.mode === "autoresearch" : item.mode !== "autoresearch"),
    );
    setProjectId(next?.project_id || "");
  }

  async function openProjectPath() {
    if (!projectId) return;
    try {
      await api<{ ok: boolean; path: string }>("/api/open-path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      toast("Opened project path", "ok");
    } catch (caught) {
      toast(caught instanceof Error ? caught.message : String(caught), "bad");
    }
  }

  async function openProjectReport() {
    if (!projectId) return;
    setLoading("project-report");
    setError("");
    try {
      const data = await api<ProjectReportPayload>(projectPath("/api/project-report"));
      setPanel({ kind: "project-report", data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function loadStatus(id = projectId) {
    if (!id) {
      setJob({ active: false, job: null });
      setRuns([]);
      return;
    }
    const data = await api<{ run: JobState; runs: Run[] }>(
      projectPath("/api/status?limit=100", id),
    );
    setJob(data.run);
    setRuns(data.runs || []);
    if (pendingSelection.current) {
      const runId =
        data.run.job?.run_id ||
        data.run.run?.run_id ||
        data.run.run_id;
      if (runId) {
        const allocated = data.runs.find((item) => item.run_id === runId) || {
          run_id: runId,
          status: data.run.job?.status || "queued",
        };
        pendingSelection.current = false;
        openRun(allocated);
      } else if (!data.run.active && data.run.job) {
        pendingSelection.current = false;
      }
    }
  }

  async function loadResearch(id = projectId) {
    if (!id) return;
    const data = await api<ResearchState>(projectPath("/api/research", id));
    setResearch(data);
    setPanel((current) => {
      if (current?.kind === "preflight") return { kind: "preflight", data };
      if (current?.kind !== "attempt") return current;
      const updated = data.experiments.find((item) => attemptKey(item) === attemptKey(current.experiment));
      return updated ? { ...current, experiment: updated, objective: data.objective } : current;
    });
  }

  async function refresh() {
    setError("");
    try {
      const id = await loadProjects(projectId);
      await loadStatus(id);
      if (isResearch) await loadResearch(id);
      toast("Refreshed");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  useEffect(() => {
    loadProjects().catch((caught) =>
      setError(caught instanceof Error ? caught.message : String(caught)),
    );
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem("autoexp-theme", theme);
  }, [theme]);

  useEffect(() => {
    setPanel(null);
    setSelectedRun("");
    pendingSelection.current = false;
    setQuery("");
    setResearch(null);
    loadStatus(projectId).catch((caught) =>
      setError(caught instanceof Error ? caught.message : String(caught)),
    );
    if (isResearch) {
      loadResearch(projectId).catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
    }
    const timer = window.setInterval(() => {
      loadStatus(projectId).catch(() => undefined);
      if (isResearch) loadResearch(projectId).catch(() => undefined);
    }, 1400);
    return () => window.clearInterval(timer);
  }, [projectId, isResearch]);

  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if (event.key === "/" && document.activeElement !== searchRef.current) {
        event.preventDefault();
        searchRef.current?.focus();
      } else if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        if (project?.exists && !active && researchReady) {
          if (isResearch) void startResearchLoop();
          else void startRun();
        }
      } else if (event.key === "Escape" && panel) {
        pendingSelection.current = false;
        setPanel(null);
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [project, active, isResearch, panel, researchReady]);

  async function startRun() {
    if (!projectId) return;
    setLoading("start");
    setError("");
    try {
      const data = await api<JobState>("/api/run/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      setJob(data);
      await loadStatus(projectId);
      toast("Run started");
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      toast("Couldn't start the run", "bad");
    } finally {
      setLoading("");
    }
  }

  function openRun(run: Run, tab: StandardTab = "overview") {
    pendingSelection.current = false;
    setSelectedRun(run.run_id);
    setPanel({ kind: "run", run, tab });
  }

  async function rerun(run: Run) {
    setLoading(`trigger:${run.run_id}`);
    setError("");
    pendingSelection.current = true;
    try {
      const data = await api<RunRequestPayload>(
        projectPath(`/api/runs/${encodeURIComponent(run.run_id)}/rerun`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId }),
        },
      );
      const next = requestedRun(data);
      if (next) {
        pendingSelection.current = false;
        openRun(next);
      }
      await loadStatus(projectId);
      toast(next?.run_id ? `Created child run ${next.run_id}` : `Re-running ${run.run_id}`);
    } catch (caught) {
      pendingSelection.current = false;
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      toast("Couldn't re-run the snapshot", "bad");
    } finally {
      setLoading("");
    }
  }

  async function runSnapshot(snapshotId: string) {
    setLoading(`snapshot:${snapshotId}`);
    setError("");
    pendingSelection.current = true;
    try {
      const data = await api<RunRequestPayload>(projectPath("/api/runs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, snapshot_id: snapshotId }),
      });
      const next = requestedRun(data);
      if (next) {
        pendingSelection.current = false;
        openRun(next);
      }
      await loadStatus(projectId);
      toast(next ? `Running ${next.run_id}` : `Snapshot ${snapshotId} queued`);
    } catch (caught) {
      pendingSelection.current = false;
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      toast("Couldn't run the snapshot", "bad");
    } finally {
      setLoading("");
    }
  }

  async function stopRun() {
    if (!projectId) return;
    setLoading("stop");
    setError("");
    try {
      setJob(
        await api<JobState>("/api/run/kill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId }),
        }),
      );
      toast("Run stopped");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function openInstruction() {
    if (!projectId) return;
    pendingSelection.current = false;
    setLoading("instruction");
    setError("");
    try {
      const data = await api<InstructionPayload>(
        projectPath("/api/report/instruction"),
      );
      setPanel({ kind: "instruction", data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function openParams() {
    if (!projectId) return;
    pendingSelection.current = false;
    setLoading("params");
    setError("");
    try {
      const data = await api<ParamsPayload>(projectPath("/api/script/params"));
      setPanel({ kind: "params", data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function saveScript(
    runId: string,
    snapshotId: string | null,
    path: string,
    text: string,
    saveAs: string,
  ) {
    const result = await api<ScriptSavePayload>("/api/script/file", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId,
        run_id: runId,
        ...(snapshotId ? { snapshot_id: snapshotId } : {}),
        path,
        text,
        save_as: saveAs,
      }),
    });
    toast(`Saved ${saveAs} as ${result.snapshot.snapshot_id}`, "ok");
    return result;
  }

  async function saveInstruction(text: string) {
    const data = await api<InstructionPayload>("/api/report/instruction", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, text }),
    });
    setPanel({ kind: "instruction", data });
    toast("Instruction saved", "ok");
    return data;
  }

  async function saveParams(params: Record<string, unknown>) {
    const data = await api<ParamsPayload>("/api/script/params", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, params }),
    });
    setPanel({ kind: "params", data });
    toast(
      data.snapshot ? `Params saved as ${data.snapshot.snapshot_id}` : "Params saved",
      "ok",
    );
    return data;
  }

  async function startResearchLoop() {
    if (!projectId || !research?.preflight.ok) return;
    setLoading("research-loop");
    setError("");
    try {
      await api<JobState>("/api/research/loop/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      await loadResearch(projectId);
      toast("Autoresearch loop started");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      await loadResearch(projectId).catch(() => undefined);
    } finally {
      setLoading("");
    }
  }

  async function stopResearchLoop() {
    if (!projectId) return;
    setLoading("research-loop");
    setError("");
    try {
      await api<JobState>("/api/research/loop/kill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      await loadResearch(projectId);
      toast("Autoresearch loop stopped");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function openResearchFile(path: string) {
    setLoading(`research-file:${path}`);
    setError("");
    try {
      const data = await api<ResearchFilePayload>(
        projectPath(`/api/research/file?path=${encodeURIComponent(path)}`),
      );
      setPanel(data.role === "frozen" ? { kind: "evaluator", data } : { kind: "program", data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  function openResearchAttempt(experiment: ResearchExperiment) {
    const key = attemptKey(experiment);
    setSelectedRun(key);
    setPanel({ kind: "attempt", experiment, objective: research!.objective });
  }

  async function saveResearchFile(path: string, text: string) {
    const data = await api<ResearchFilePayload>("/api/research/file", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, path, text }),
    });
    setPanel(data.role === "frozen" ? { kind: "evaluator", data } : { kind: "program", data });
    await loadResearch(projectId);
    toast(data.snapshot ? `Saved ${path} as ${data.snapshot.snapshot_id}` : `Saved ${path}`, "ok");
    return data;
  }

  async function importResearchBaseline(file: File) {
    if (!projectId) return;
    setLoading("research-import");
    setError("");
    try {
      const text = await file.text();
      const data = await api<ResearchFilePayload>("/api/research/file", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, path: "experiment/candidate.py", text }),
      });
      await loadResearch(projectId);
      setPanel({ kind: "program", data });
      toast(`Imported ${file.name} as ${data.path}`, "ok");
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      toast("Couldn't import baseline", "bad");
    } finally {
      setLoading("");
    }
  }

  const filteredRuns = runs.filter((run) => {
    const text =
      `${run.title || ""} ${run.run_id} ${run.status} ${(run.changes || []).join(" ")} ${run.script_name || ""} ${run.report_path || ""} ${(run.output_files || []).join(" ")}`.toLowerCase();
    return text.includes(query.toLowerCase());
  });
  const metrics = {
    total: runs.length,
    completed: runs.filter((run) => run.status === "success").length,
    failed: runs.filter((run) => run.status === "failed").length,
    reports: runs.filter((run) => run.report_path).length,
  };

  return (
    <>
      <div className="shell">
        <div className="sticky-stack">
        <header className="topbar">
          <div className="wordmark">
            <div className="logo-lockup">
              <b>auto<i>exp</i></b>
              {researchView ? (
                <><span className="logo-divider">|</span><span className="submark">auto<i>research</i></span></>
              ) : null}
            </div>
            <span className="tag">local autonomous experimentation</span>
          </div>
          <div className="bar-spacer" />
          <div className="bar-tools">
            <div className="mode-switch" role="group" aria-label="Project mode">
              <button
                className={projectMode === "standard" ? "active" : ""}
                onClick={() => switchProjectMode("standard")}
                aria-pressed={projectMode === "standard"}
              >
                Standard
              </button>
              <button
                className={projectMode === "autoresearch" ? "active research" : ""}
                onClick={() => switchProjectMode("autoresearch")}
                aria-pressed={projectMode === "autoresearch"}
              >
                Autoresearch
              </button>
            </div>
            <label className="picker">
              <FolderOpen size={15} />
              <span className="lbl">project</span>
              <select
                value={projectId}
                title={project ? `${project.path} · runner ${project.runner || "local"}` : "Select a project"}
                onChange={(event) => setProjectId(event.target.value)}
                disabled={!hasAvailableProjects}
              >
                {!hasAvailableProjects ? (
                  <option value="">No available {researchView ? "autoresearch" : "standard"} projects</option>
                ) : null}
                {filteredProjects.map((item) => (
                  <option
                    key={item.project_id}
                    value={item.project_id}
                    disabled={!item.exists}
                  >
                    {item.title}{item.exists ? "" : " · missing"}
                  </option>
                ))}
              </select>
              <ChevronDown className="chev" size={15} />
            </label>
            {project ? (
              <button className="btn open-path" onClick={openProjectPath} title={project.path}>
                <FolderOpen size={14} /> Open path
              </button>
            ) : null}
            {project ? (
              <button className="btn open-path" onClick={openProjectReport} title="Project report">
                <FileText size={14} /> Project report
              </button>
            ) : null}
            <span className={`lamp${active ? " live" : ""}`}>
              <span className="dot" />
              {active
                ? isResearch
                  ? `loop ${research?.loop.phase || "running"}`
                  : `run ${job.job?.status || "running"}`
                : "idle"}
            </span>
            {project ? <span className="bar-divider" /> : null}
            {project && !isResearch ? (
              <div className="quick-actions">
                {job.active ? (
                  <button className="btn danger" onClick={stopRun} disabled={loading === "stop"}>
                    <Square size={14} fill="currentColor" /> Stop
                  </button>
                ) : (
                  <button className="btn primary" onClick={() => startRun()} disabled={loading === "start" || !project.exists} title="Run current experiment · ⌘↵">
                    <Play size={14} fill="currentColor" /> Run
                  </button>
                )}
                <button className="icon-btn" onClick={() => setPanel({ kind: "log" })} title="Worker log" aria-label="Worker log"><Terminal size={15} /></button>
                <button className="icon-btn" onClick={openParams} disabled={!project.exists} title="Parameters" aria-label="Parameters"><SlidersHorizontal size={15} /></button>
                <button className="icon-btn" onClick={openInstruction} disabled={!project.exists} title="Report guidance" aria-label="Report guidance"><FileText size={15} /></button>
              </div>
            ) : null}
            {project && isResearch && research ? (
              <div className="quick-actions">
                {research.loop.active ? (
                  <button className="btn danger" onClick={stopResearchLoop} disabled={loading === "research-loop"}><Square size={14} fill="currentColor" /> Stop</button>
                ) : (
                  <button className="btn primary" onClick={startResearchLoop} disabled={loading === "research-loop" || !research.preflight.ok} title="Start Autoresearch loop · ⌘↵"><Repeat2 size={14} /> Start loop</button>
                )}
                <button className="icon-btn" onClick={() => openResearchFile("experiment/program.md")} title="Research program" aria-label="Research program"><FileText size={15} /></button>
                <button className={`icon-btn readiness${research.preflight.ok ? " ready" : " blocked"}`} onClick={() => setPanel({ kind: "preflight", data: research })} title="Preflight checks" aria-label="Preflight checks">
                  {research.preflight.ok ? <Check size={15} /> : <AlertTriangle size={15} />}
                </button>
                <button className="icon-btn" onClick={() => setPanel({ kind: "log" })} title="Agent log" aria-label="Agent log"><Terminal size={15} /></button>
              </div>
            ) : null}
            <button className="icon-btn" onClick={refresh} title="Refresh" aria-label="Refresh">
              <RefreshCw size={16} />
            </button>
            <button
              className="icon-btn"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              title="Toggle theme"
              aria-label="Toggle theme"
            >
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
          </div>
        </header>

        </div>

        {error ? (
          <div className="err-banner">
            <AlertTriangle size={15} /> {error}
          </div>
        ) : null}

        {displayedResearch ? (
          <ResearchDashboard
            research={displayedResearch}
            selected={selectedRun}
            panel={project ? panel : null}
            leftColumn={leftColumn}
            theme={theme}
            projectId={projectId}
            preview={!project}
            onSelect={setSelectedRun}
            onExperiment={openResearchAttempt}
            onOpenFile={openResearchFile}
            onResize={setLeftColumn}
            onSaveFile={saveResearchFile}
            onImportBaseline={importResearchBaseline}
            importing={loading === "research-import"}
            onClose={() => setPanel(null)}
          />
        ) : null}

        {researchView && !project ? (
          <>
            <section className="strip compact-strip">
              <Stat label="Experiments" value={0} tone="runs" />
              <Stat label="Kept" value={0} tone="pass" />
              <Stat label="Reverted" value={0} tone="rep" />
              <div className="stat pass"><span className="k">Improvement</span><span className="v">—</span></div>
            </section>
            <section className="card research-empty-state">
              <Target size={22} />
              <div><h2>No Autoresearch project</h2><p>Create one to establish a baseline and begin the ratchet.</p></div>
              <span className="cmd"><b>autoexp init</b> metric_lab --autoresearch</span>
            </section>
          </>
        ) : null}

        {!researchView ? <>
        <section className="strip">
          <Stat label="Runs" value={metrics.total} tone="runs" />
          <Stat label="Completed" value={metrics.completed} tone="pass" total={metrics.total} />
          <Stat label="Failed" value={metrics.failed} tone="fail" total={metrics.total} />
          <Stat label="Reports" value={metrics.reports} tone="rep" total={metrics.total} />
        </section>

        <section
          className={`work${panel ? " split" : ""}`}
          style={panel ? ({ "--lcol": `${leftColumn}%` } as React.CSSProperties) : undefined}
        >
          <div className="card">
            <div className="card-head">
              <div className="card-head-copy">
                <div className="eyebrow">ledger</div>
                <h2>Recent runs</h2>
                {project ? <span className="sub">Immutable source, parameters, output, and report for every run.</span> : null}
              </div>
              <div className="search">
                <Search className="si" size={15} />
                <input
                  ref={searchRef}
                  placeholder="Filter runs"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  disabled={!project}
                />
                <span className="kbd">/</span>
              </div>
            </div>
            <RunLedger
              runs={filteredRuns}
              selected={selectedRun}
              loading={loading}
              active={job.active}
              project={project}
              onOpen={openRun}
              onTrigger={rerun}
              onCopy={(runId) => {
                void navigator.clipboard?.writeText(runId);
                toast("Run ID copied");
              }}
            />
            {project ? (
              <div className="hint-row ledger-hints">
                <span><kbd>/</kbd> filter</span>
                <span><kbd>⌘</kbd><kbd>↵</kbd> run</span>
                <span><kbd>esc</kbd> close panel</span>
                <span className="server-mode">live · autoexp server</span>
              </div>
            ) : null}
          </div>

          {panel ? (
            <SplitHandle value={leftColumn} onChange={setLeftColumn} />
          ) : null}
          {panel ? panel.kind === "run" ? (
            <StandardInspector
              run={panel.run}
              tab={panel.tab}
              theme={theme}
              projectId={projectId}
              rerunning={loading === `trigger:${panel.run.run_id}`}
              runningSnapshot={loading.startsWith("snapshot:")}
              onTab={(tab) => setPanel({ ...panel, tab })}
              onRerun={rerun}
              onRunSnapshot={runSnapshot}
              onSaveScript={saveScript}
              onClose={() => {
                pendingSelection.current = false;
                setPanel(null);
              }}
            />
          ) : (
            <Viewer
              panel={panel}
              projectId={projectId}
              active={job.active}
              onSaveInstruction={saveInstruction}
              onSaveParams={saveParams}
              onClose={() => setPanel(null)}
            />
          ) : null}
        </section>
        </> : null}
      </div>

      <div className="toasts">
        {toasts.map((item) => (
          <div key={item.id} className={`toast${item.kind ? ` ${item.kind}` : ""}`}>
            <span className="ti">
              {item.kind === "ok" ? (
                <Check size={15} />
              ) : item.kind === "bad" ? (
                <AlertTriangle size={15} />
              ) : (
                <RefreshCw size={15} />
              )}
            </span>
            {item.message}
          </div>
        ))}
      </div>
    </>
  );
}

function ResearchDashboard({
  research,
  selected,
  panel,
  leftColumn,
  theme,
  projectId,
  preview = false,
  onSelect,
  onExperiment,
  onOpenFile,
  onResize,
  onSaveFile,
  onImportBaseline,
  importing,
  onClose,
}: {
  research: ResearchState;
  selected: string;
  panel: Panel | null;
  leftColumn: number;
  theme: Theme;
  projectId: string;
  preview?: boolean;
  onSelect: (tag: string) => void;
  onExperiment: (experiment: ResearchExperiment) => void;
  onOpenFile: (path: string) => void;
  onResize: (value: number) => void;
  onSaveFile: (path: string, text: string) => Promise<ResearchFilePayload>;
  onImportBaseline: (file: File) => Promise<void>;
  importing?: boolean;
  onClose: () => void;
}) {
  const kept = research.experiments.filter((item) => attemptVerdict(item) === "kept").length;
  const reverted = research.experiments.filter((item) => attemptVerdict(item) === "reverted").length;
  const { baseline, best, direction } = research.objective;
  const improvement =
    baseline === null || best === null
      ? null
      : direction === "min"
        ? baseline - best
        : best - baseline;

  return (
    <>
      <section className="strip">
        <Stat label="Experiments" value={research.experiments.length} tone="runs" />
        <Stat label="Kept" value={kept} tone="pass" />
        <Stat label="Reverted" value={reverted} tone="rep" />
        <div className="stat pass">
          <span className="k">Improvement vs baseline</span>
          <span className="v research-delta">
            {improvement === null ? "—" : `+${formatScore(improvement)}`}
          </span>
        </div>
      </section>

      <div className="card chart-card">
        <div className="card-head">
          <div>
            <div className="eyebrow">objective</div>
            <h2>Ratchet · {research.objective.metric} over attempts</h2>
          </div>
          <div className="chart-legend">
            <span className="lg"><span className="sw best" /> best-so-far</span>
            <span className="lg"><span className="sw kept" /> kept</span>
            <span className="lg"><span className="sw rev" /> reverted</span>
          </div>
        </div>
        <RatchetChart
          experiments={research.experiments}
          objective={research.objective}
          emptyText={preview ? "Create a project to establish a baseline and begin the ratchet." : undefined}
        />
      </div>

      {research.can_import_baseline && !preview ? (
        <BaselineImport onImport={onImportBaseline} disabled={importing} />
      ) : null}

      <div className="card contract-card">
        <div
          className="card-head contract-bar"
          title={`${research.contract.contract_id} · ${research.contract.status} · evaluator ${research.contract.evaluator_hash || "unavailable"}`}
        >
          <div>
            <div className="eyebrow">contract</div>
            <h2>Files &amp; ownership</h2>
          </div>
          <ResearchContract files={research.files} onOpen={onOpenFile} disabled={preview} />
        </div>
      </div>

      <section
        className={`work${panel ? " split" : ""}`}
        style={panel ? ({ "--lcol": `${leftColumn}%` } as React.CSSProperties) : undefined}
      >
        <div className="card">
          <div className="card-head">
            <div>
              <div className="eyebrow">ledger</div>
              <h2>Experiments</h2>
            </div>
            <span className="sub">Every hypothesis remains visible, whether kept or reverted</span>
          </div>
          <ExperimentLedger
            experiments={research.experiments}
            objective={research.objective}
            selected={selected}
            onSelect={onSelect}
            onOpen={onExperiment}
            preview={preview}
          />
          <div className="hint-row ledger-hints">
            <span>
              {preview
                ? "template preview"
                : research.loop.active
                ? `loop ${research.loop.status || "running"} · ${research.loop.phase}${research.loop.session_id ? ` · ${research.loop.session_id}` : ""}`
                : `loop ${research.loop.status || "idle"}`}
            </span>
            <span className="server-mode">{preview ? "autoexp init metric_lab --autoresearch" : "live · autoexp server"}</span>
          </div>
        </div>
        {panel ? <SplitHandle value={leftColumn} onChange={onResize} /> : null}
        {panel ? (
          <ResearchViewer
            panel={panel}
            theme={theme}
            projectId={projectId}
            active={research.loop.active}
            onSaveFile={onSaveFile}
            onClose={onClose}
          />
        ) : null}
      </section>
    </>
  );
}

function RatchetChart({
  experiments,
  objective,
  emptyText,
}: {
  experiments: ResearchExperiment[];
  objective: ResearchObjective;
  emptyText?: string;
}) {
  const chronological = [...experiments].reverse();
  const fullMax = Math.max(chronological.length - 1, 1);
  const [domain, setDomain] = useState<[number, number]>([0, fullMax]);
  const [hovered, setHovered] = useState<number | null>(null);
  const drag = useRef<{ x: number; start: number; end: number } | null>(null);
  useEffect(() => setDomain([0, fullMax]), [fullMax]);
  const scored = chronological.filter(
    (item): item is ResearchExperiment & { score: number } => item.score !== null,
  );
  if (!scored.length) {
    return (
      <div className="empty chart-empty">
        {emptyText || "Start the loop to establish a baseline and begin the ratchet."}
      </div>
    );
  }

  const baseline = objective.baseline ?? scored[0].score;
  const best = objective.best ?? baseline;
  const visible = chronological.filter((item, index) => item.score !== null && index >= domain[0] && index <= domain[1]);
  const values = visible.length ? visible.map((item) => item.score as number) : scored.map((item) => item.score);
  const low = Math.min(...values, baseline, best);
  const high = Math.max(...values, baseline, best);
  const padding = (high - low) * 0.18 || Math.max(Math.abs(high) * 0.02, 0.01);
  const min = low - padding;
  const max = high + padding;
  const width = 820;
  const height = 270;
  const left = 68;
  const right = 24;
  const top = 18;
  const bottom = 42;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const x = (index: number) =>
    chronological.length === 1
      ? left + plotWidth / 2
      : left + ((index - domain[0]) / Math.max(domain[1] - domain[0], 1)) * plotWidth;
  const y = (value: number) =>
    top + ((max - value) / (max - min)) * plotHeight;

  let currentBest: number | null = objective.baseline;
  const step: [number, number][] = [];
  chronological.forEach((item, index) => {
    if (item.score !== null && attemptVerdict(item) === "kept") {
      currentBest =
        currentBest === null
          ? item.score
          : objective.direction === "min"
            ? Math.min(currentBest, item.score)
            : Math.max(currentBest, item.score);
    }
    if (currentBest !== null && index >= domain[0] && index <= domain[1]) step.push([x(index), y(currentBest)]);
  });
  const path = step
    .map((point, index) =>
      index === 0
        ? `M ${point[0]} ${point[1]}`
        : `L ${point[0]} ${step[index - 1][1]} L ${point[0]} ${point[1]}`,
    )
    .join(" ");
  const ticks = Array.from({ length: 5 }, (_, index) => max - ((max - min) * index) / 4);
  const firstAttempt = Math.max(0, Math.ceil(domain[0]));
  const lastAttempt = Math.min(chronological.length - 1, Math.floor(domain[1]));
  const xStep = Math.max(1, Math.ceil((lastAttempt - firstAttempt + 1) / 6));
  const xTicks = Array.from(
    { length: Math.max(0, Math.floor((lastAttempt - firstAttempt) / xStep) + 1) },
    (_, index) => firstAttempt + index * xStep,
  );
  if (lastAttempt >= firstAttempt && xTicks[xTicks.length - 1] !== lastAttempt) xTicks.push(lastAttempt);
  const lastStep = step[step.length - 1];
  const scorePath = chronological
    .map((item, index) => item.score === null || index < domain[0] || index > domain[1] ? null : [x(index), y(item.score)] as const)
    .filter((point): point is readonly [number, number] => point !== null)
    .map((point, index) => `${index ? "L" : "M"} ${point[0]} ${point[1]}`)
    .join(" ");

  function updateDomain(start: number, end: number) {
    const span = Math.min(fullMax, Math.max(Math.min(2, fullMax), end - start));
    const nextStart = Math.max(0, Math.min(start, fullMax - span));
    setDomain([nextStart, nextStart + span]);
  }

  function zoom(factor: number, center = (domain[0] + domain[1]) / 2) {
    const span = domain[1] - domain[0];
    const nextSpan = span * factor;
    const ratio = span ? (center - domain[0]) / span : 0.5;
    updateDomain(center - nextSpan * ratio, center + nextSpan * (1 - ratio));
  }

  const hoveredItem = hovered === null ? null : chronological[hovered];
  const hoverX = hovered === null ? 0 : x(hovered);
  const hoverY = hoveredItem?.score === null || hoveredItem?.score === undefined ? 0 : y(hoveredItem.score);
  const tooltipX = Math.min(width - right - 238, Math.max(left + 8, hoverX + (hoverX > width * 0.65 ? -246 : 12)));
  const tooltipY = Math.min(height - bottom - 88, Math.max(top + 8, hoverY - 42));

  return (
    <div className="chart-wrap">
      <div className="chart-toolbar">
        <span>Wheel to zoom · drag to pan · hover for details</span>
        <div>
          <button onClick={() => zoom(0.72)} disabled={domain[1] - domain[0] <= Math.min(2, fullMax)} aria-label="Zoom in"><ZoomIn size={14} /></button>
          <button onClick={() => zoom(1.38)} disabled={domain[0] === 0 && domain[1] === fullMax} aria-label="Zoom out"><ZoomOut size={14} /></button>
          <button onClick={() => setDomain([0, fullMax])} disabled={domain[0] === 0 && domain[1] === fullMax}>Reset</button>
        </div>
      </div>
      <svg
        className={`ratchet${drag.current ? " panning" : ""}`}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="Interactive objective score and best-so-far by attempt"
        onWheel={(event) => {
          event.preventDefault();
          const box = event.currentTarget.getBoundingClientRect();
          const pointer = ((event.clientX - box.left) / box.width) * width;
          const ratio = Math.max(0, Math.min(1, (pointer - left) / plotWidth));
          zoom(event.deltaY > 0 ? 1.18 : 0.82, domain[0] + ratio * (domain[1] - domain[0]));
        }}
        onPointerDown={(event) => {
          drag.current = { x: event.clientX, start: domain[0], end: domain[1] };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (!drag.current) return;
          const box = event.currentTarget.getBoundingClientRect();
          const shift = ((event.clientX - drag.current.x) / box.width) * (drag.current.end - drag.current.start);
          updateDomain(drag.current.start - shift, drag.current.end - shift);
        }}
        onPointerUp={() => { drag.current = null; }}
        onPointerCancel={() => { drag.current = null; }}
        onPointerLeave={() => setHovered(null)}
      >
        <defs>
          <clipPath id="ratchet-plot"><rect x={left} y={top} width={plotWidth} height={plotHeight} rx="4" /></clipPath>
          <linearGradient id="ratchet-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--pass)" stopOpacity=".16" />
            <stop offset="1" stopColor="var(--pass)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <rect className="plot-bg" x={left} y={top} width={plotWidth} height={plotHeight} rx="4" />
        {ticks.map((tick) => (
          <g key={tick}>
            <line className="gl" x1={left} y1={y(tick)} x2={width - right} y2={y(tick)} />
            <text className="tick" x={left - 8} y={y(tick) + 3} textAnchor="end">{formatScore(tick)}</text>
          </g>
        ))}
        {xTicks.map((index) => (
          <g key={index}>
            <line className="xgl" x1={x(index)} y1={top} x2={x(index)} y2={height - bottom} />
            <text className="tick" x={x(index)} y={height - bottom + 18} textAnchor="middle">{attemptLabel(chronological[index])}</text>
          </g>
        ))}
        <g clipPath="url(#ratchet-plot)">
          <line className="base" x1={left} y1={y(baseline)} x2={width - right} y2={y(baseline)} />
          {lastStep && path ? <path className="step-area" d={`${path} L ${lastStep[0]} ${height - bottom} L ${step[0][0]} ${height - bottom} Z`} /> : null}
          <path className="score-line" d={scorePath} />
          <path className="step" d={path} />
        </g>
        <text className="baseline-label" x={width - right - 5} y={Math.max(top + 11, y(baseline) - 6)} textAnchor="end">
          baseline {formatScore(baseline)}
        </text>
        {chronological.map((item, index) =>
          item.score === null ? null : (
            <circle
              key={attemptKey(item)}
              className={`point ${attemptVerdict(item) === "kept" ? "kept" : "rev"}${hovered === index ? " hovered" : ""}`}
              cx={x(index)}
              cy={y(item.score)}
              r={hovered === index ? 6 : attemptVerdict(item) === "kept" ? 4.5 : 4}
              visibility={index >= domain[0] && index <= domain[1] ? "visible" : "hidden"}
              onPointerEnter={(event) => { event.stopPropagation(); setHovered(index); }}
            />
          ),
        )}
        {lastStep ? (
          <text className="blabel" x={lastStep[0]} y={lastStep[1] - 7} textAnchor="end">
            best {formatScore(best)}
          </text>
        ) : null}
        <text className="axis-label" x={(left + width - right) / 2} y={height - 5} textAnchor="middle">Attempt</text>
        <text className="axis-label" transform={`translate(14 ${(top + height - bottom) / 2}) rotate(-90)`} textAnchor="middle">{objective.metric}</text>
        {hoveredItem?.score !== null && hoveredItem?.score !== undefined ? (
          <g className="chart-tooltip" transform={`translate(${tooltipX} ${tooltipY})`} pointerEvents="none">
            <rect width="238" height="80" rx="7" />
            <text x="12" y="18" className="tooltip-title">{attemptLabel(hoveredItem)} · {attemptVerdict(hoveredItem) || attemptState(hoveredItem)}</text>
            <text x="12" y="37">Score <tspan>{formatScore(hoveredItem.score)}</tspan></text>
            <text x="126" y="37">Change <tspan>{hoveredItem.improvement == null ? "—" : `${hoveredItem.improvement > 0 ? "+" : ""}${formatScore(hoveredItem.improvement)}`}</tspan></text>
            <text x="12" y="59" className="tooltip-hypothesis">{attemptHypothesis(hoveredItem).slice(0, 48)}</text>
          </g>
        ) : null}
      </svg>
    </div>
  );
}

function BaselineImport({
  onImport,
  disabled = false,
}: {
  onImport: (file: File) => Promise<void>;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  async function pick(files: FileList | null) {
    const file = files?.[0];
    if (file) await onImport(file);
  }

  return (
    <div
      className={`card baseline-import${over ? " over" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        if (!disabled) void pick(event.dataTransfer.files);
      }}
    >
      <div>
        <div className="eyebrow">baseline</div>
        <h2>Upload your autoresearch loop script</h2>
        <p>Drop a Python script here to import it as <span className="mono-inline">experiment/candidate.py</span>.</p>
      </div>
      <button className="btn" disabled={disabled} onClick={() => inputRef.current?.click()}>
        <Upload size={15} /> {disabled ? "Importing…" : "Choose file"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept=".py,text/x-python,text/plain"
        hidden
        onChange={(event) => {
          void pick(event.target.files);
          event.currentTarget.value = "";
        }}
      />
    </div>
  );
}

function ResearchContract({
  files,
  onOpen,
  disabled = false,
}: {
  files: ResearchState["files"];
  onOpen: (path: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="contract">
      {files.map((file) => (
        <button
          className={`contract-file ${file.role}`}
          key={file.path}
          onClick={() => onOpen(file.path)}
          disabled={disabled}
          title={`${file.path} · ${file.desc}${file.hash ? ` · ${file.hash}` : ""}`}
        >
          <span className={`role ${file.role}`}>
            {file.role === "frozen" ? <Lock size={11} /> : file.role === "agent" ? <Bot size={12} /> : null}
            {file.role}
          </span>
          <span className="fname">{file.path.split("/").pop()}</span>
          {file.role === "frozen" ? <Lock size={12} /> : <Pencil size={12} />}
        </button>
      ))}
    </div>
  );
}

function ResearchPreflight({ research, embedded = false }: { research: ResearchState; embedded?: boolean }) {
  const failed = research.preflight.checks.filter((check) => check.required && !check.ok);
  return (
    <div className={`${embedded ? "preflight-card embedded" : "card preflight-card"}${research.preflight.ok ? " ready" : " blocked"}`}>
      <div className="card-head">
        <div>
          <div className="eyebrow">preflight</div>
          <h2>{research.preflight.ok ? "Ready to start" : failed.length ? `${failed.length} required check${failed.length === 1 ? "" : "s"} failed` : "Preflight unavailable"}</h2>
        </div>
        <span className={`preflight-state ${research.preflight.ok ? "ok" : "bad"}`}>
          {research.preflight.ok ? <Check size={13} /> : <AlertTriangle size={13} />}
          {research.preflight.ok ? "ready" : "start disabled"}
        </span>
      </div>
      <div className="preflight-checks">
        {research.preflight.checks.map((check) => (
          <div className={`preflight-check${check.ok ? " ok" : check.required ? " bad" : " warn"}`} key={check.name}>
            <span className="preflight-icon">{check.ok ? "✓" : "!"}</span>
            <span><b>{humanize(check.name)}</b><small>{check.detail}</small></span>
            {!check.required ? <em>optional</em> : null}
          </div>
        ))}
        {!research.preflight.checks.length ? <div className="evidence-empty">No preflight results are available.</div> : null}
      </div>
      {research.loop.failure_message ? (
        <div className="preflight-failure"><AlertTriangle size={13} /> {research.loop.failure_message}</div>
      ) : null}
    </div>
  );
}

function ExperimentLedger({
  experiments,
  objective,
  selected,
  onSelect,
  onOpen,
  preview = false,
}: {
  experiments: ResearchExperiment[];
  objective: ResearchObjective;
  selected: string;
  onSelect: (tag: string) => void;
  onOpen: (experiment: ResearchExperiment) => void;
  preview?: boolean;
}) {
  if (!experiments.length) {
    if (preview) {
      return (
        <div className="empty">
          <div className="big">No attempts yet</div>
          <div>Create an Autoresearch project to activate this workspace.</div>
          <div className="cmd"><b>autoexp init</b> metric_lab --autoresearch</div>
        </div>
      );
    }
    return <Empty title="No attempts yet">Start the loop and the agent will begin proposing experiments.</Empty>;
  }
  return (
    <div className="ledger-wrap">
      <table className="ledger research-ledger">
        <thead>
          <tr>
            <th>Attempt</th>
            <th>Result</th>
            <th>{objective.metric}</th>
            <th>Improvement</th>
            <th>Hypothesis</th>
            <th>Snapshot</th>
          </tr>
        </thead>
        <tbody>
          {experiments.map((item) => {
            const key = attemptKey(item);
            const label = attemptLabel(item);
            const verdict = attemptVerdict(item);
            const state = attemptState(item);
            const result = verdict || state;
            const improvement = item.improvement;
            return (
              <tr
                key={key}
                className={`${verdict === "reverted" ? "rev-row " : ""}${selected === key ? "sel" : ""}`}
                tabIndex={0}
                aria-label={`Inspect research attempt ${label}`}
                onClick={() => {
                  onSelect(key);
                  onOpen(item);
                }}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
                  event.preventDefault();
                  onSelect(key);
                  onOpen(item);
                }}
              >
                <td><span className="tagcell">{label}</span></td>
                <td><span className={`status ${result}`}><span className="d" />{result}</span></td>
                <td>{item.score === null ? <span className="dash">—</span> : <span className="score">{formatScore(item.score)}</span>}</td>
                <td>{improvement === null || improvement === undefined ? <span className="dash">—</span> : <span className={`delta ${improvement > 0 ? "good" : "flat"}`}>{improvement > 0 ? "+" : ""}{formatScore(improvement)}</span>}</td>
                <td><span className="hyp">{attemptHypothesis(item)}</span></td>
                <td>
                  {item.candidate_snapshot_id ? (
                    <span className="commitcell"><GitCommit size={13} /> {item.candidate_snapshot_id}</span>
                  ) : item.commit ? (
                    <span className="commitcell"><GitCommit size={13} /> {item.commit}</span>
                  ) : state === "running" ? (
                    <span className="commitcell"><GitBranch size={13} /> candidate pending</span>
                  ) : (
                    <span className="dash">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ResearchViewer({
  panel,
  theme,
  projectId,
  active,
  onSaveFile,
  onClose,
}: {
  panel: Panel;
  theme: Theme;
  projectId: string;
  active: boolean;
  onSaveFile: (path: string, text: string) => Promise<ResearchFilePayload>;
  onClose: () => void;
}) {
  if (panel.kind === "attempt") {
    return (
      <ResearchAttemptInspector
        experiment={panel.experiment}
        objective={panel.objective}
        projectId={projectId}
        onClose={onClose}
      />
    );
  }
  if (panel.kind === "preflight") {
    return (
      <aside className="card panel">
        <div className="card-head">
          <div className="ptitle"><span className="ic"><Check size={16} /></span><span className="txt">Preflight checks</span></div>
          <button className="btn ghost sm" onClick={onClose} aria-label="Close panel"><X size={16} /></button>
        </div>
        <div className="panel-body scroll-pane"><ResearchPreflight research={panel.data} embedded /></div>
      </aside>
    );
  }
  if (panel.kind === "project-report") {
    return (
      <aside className="card panel">
        <div className="card-head">
          <div className="ptitle"><span className="ic"><FileText size={16} /></span><span className="txt">Project report</span></div>
          <button className="btn ghost sm" onClick={onClose} aria-label="Close panel"><X size={16} /></button>
        </div>
        <div className="panel-body"><RunReport data={panel.data} project /></div>
      </aside>
    );
  }
  const title =
    panel.kind === "program"
      ? `${panel.data.path} · ${panel.data.role}-owned`
      : panel.kind === "evaluator"
        ? `${panel.data.path} · ${panel.data.role}`
        : "Agent log";
  const Icon =
    panel.kind === "program"
      ? panel.data.role === "agent" ? Bot : FileText
      : panel.kind === "evaluator"
        ? panel.data.role === "frozen" ? Lock : Code2
        : Terminal;
  return (
    <aside className="card panel">
      <div className="card-head">
        <div className="ptitle"><span className="ic"><Icon size={16} /></span><span className="txt">{title}</span></div>
        <button className="btn ghost sm" onClick={onClose} aria-label="Close panel"><X size={16} /></button>
      </div>
      <div className="panel-body">
        {panel.kind === "program" ? (
          <TextEditor value={panel.data.text} label={panel.data.path} onSave={(text) => onSaveFile(panel.data.path, text)} />
        ) : null}
        {panel.kind === "evaluator" ? <ResearchFileViewer data={panel.data} /> : null}
        {panel.kind === "log" ? <LogViewer projectId={projectId} active={active} kind="agent" /> : null}
      </div>
    </aside>
  );
}

function ResearchFileViewer({ data }: { data: ResearchFilePayload }) {
  return (
    <div className="editor-shell">
      <div className="frozenbar">
        {data.role === "frozen" ? <Lock size={13} /> : <Code2 size={13} />}
        {data.role} · {data.hash || data.path}
        {data.role === "frozen" ? <span className="ok"><Lock size={13} /> fixed evaluator</span> : null}
      </div>
      <CodePane value={data.text} editing={false} onChange={() => undefined} />
    </div>
  );
}

function ResearchAttemptInspector({
  experiment,
  objective,
  projectId,
  onClose,
}: {
  experiment: ResearchExperiment;
  objective: ResearchObjective;
  projectId: string;
  onClose: () => void;
}) {
  const key = attemptKey(experiment);
  const label = attemptLabel(experiment);
  const verdict = attemptVerdict(experiment);
  const state = attemptState(experiment);
  const [tab, setTab] = useState<ResearchTab>("diff");
  const [overview, setOverview] = useState<RunAggregate | null>(null);
  const [diff, setDiff] = useState<RunDiffPayload | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [artifactDetail, setArtifactDetail] = useState<ArtifactDetailPayload | null>(null);
  const [pending, setPending] = useState(false);
  const [paneError, setPaneError] = useState("");

  useEffect(() => {
    setTab("diff");
    setOverview(null);
    setDiff(null);
    setArtifacts([]);
    setSelectedArtifact(null);
    setArtifactDetail(null);
    setPaneError("");
  }, [key]);

  useEffect(() => {
    let live = true;
    setPending(true);
    setPaneError("");
    const request = tab === "diff"
      ? api<RunDiffPayload>(projectUrl(`/api/research/diff?attempt_id=${encodeURIComponent(key)}`, projectId))
          .then((data) => { if (live) setDiff(data); })
      : !experiment.run_id
        ? Promise.resolve()
        : tab === "run"
          ? api<RunDetailPayload>(projectUrl(`/api/runs/${encodeURIComponent(experiment.run_id)}`, projectId))
              .then((data) => {
                if (!live) return;
                setOverview(normalizeRunDetail(data, {
                  run_id: experiment.run_id!,
                  status: state === "failed" ? "failed" : state === "running" ? "running" : "success",
                  source_snapshot_id: experiment.candidate_snapshot_id,
                }));
              })
          : api<ArtifactListPayload>(projectUrl(`/api/runs/${encodeURIComponent(experiment.run_id)}/artifacts`, projectId))
              .then((data) => {
                if (!live) return;
                const items = Array.isArray(data) ? data : data.artifacts || [];
                setArtifacts(items);
                setSelectedArtifact(items[0] || null);
              });
    request.catch((caught) => {
      if (live) setPaneError(caught instanceof Error ? caught.message : String(caught));
    }).finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [experiment.candidate_snapshot_id, experiment.run_id, key, projectId, state, tab]);

  useEffect(() => {
    if (tab !== "artifacts" || !experiment.run_id || !selectedArtifact) {
      setArtifactDetail(null);
      return;
    }
    let live = true;
    setArtifactDetail(null);
    setPending(true);
    api<ArtifactDetailPayload>(projectUrl(
      `/api/runs/${encodeURIComponent(experiment.run_id)}/artifacts/${encodeURIComponent(selectedArtifact.artifact_id)}`,
      projectId,
    )).then((data) => { if (live) setArtifactDetail(data); })
      .catch((caught) => { if (live) setPaneError(caught instanceof Error ? caught.message : String(caught)); })
      .finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [experiment.run_id, projectId, selectedArtifact, tab]);

  const tabs: Array<{ id: ResearchTab; label: string; icon: LucideIcon }> = [
    { id: "diff", label: "Changes", icon: GitBranch },
    { id: "run", label: "Run", icon: FlaskConical },
    { id: "artifacts", label: "Artifacts", icon: FolderOpen },
  ];

  return (
    <aside className="card panel run-inspector research-inspector">
      <div className="run-inspector-head">
        <div className="inspector-title">
          <span className="ic"><FlaskConical size={17} /></span>
          <div>
            <div className="inspector-id">{label} · {verdict || state}</div>
            <div className="inspector-sub">{attemptHypothesis(experiment)}</div>
          </div>
        </div>
        <button className="btn ghost sm inspector-close" onClick={onClose} aria-label="Close research inspector"><X size={16} /></button>
      </div>
      <div className="attempt-summary">
        <OverviewValue label={objective.metric} value={experiment.score === null ? null : formatScore(experiment.score)} />
        <OverviewValue label="Best before" value={experiment.best_score_before === null || experiment.best_score_before === undefined ? null : formatScore(experiment.best_score_before)} />
        <OverviewValue label="Improvement" value={experiment.improvement === null || experiment.improvement === undefined ? null : formatScore(experiment.improvement)} />
      </div>
      <div className="inspector-tabs" role="tablist" aria-label="Research attempt inspector">
        {tabs.map(({ id, label: tabLabel, icon: Icon }) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            role="tab"
            aria-selected={tab === id}
            disabled={id !== "diff" && !experiment.run_id}
            onClick={() => setTab(id)}
          >
            <Icon size={13} /> {tabLabel}
          </button>
        ))}
      </div>
      <div className={`panel-body run-pane${tab === "run" ? " scroll-pane" : ""}`}>
        {paneError ? <div className="pane-error"><AlertTriangle size={14} /> {paneError}</div> : null}
        {tab === "diff" ? diff ? <RunDiff data={diff} /> : pending ? <PaneLoading /> : null : null}
        {tab === "run" ? experiment.run_id
          ? overview ? <RunOverview data={overview} onNavigate={() => setTab("artifacts")} /> : pending ? <PaneLoading /> : null
          : <Empty title="No immutable run yet">This attempt has not allocated a run.</Empty> : null}
        {tab === "artifacts" ? experiment.run_id ? (
          <ArtifactViewer
            artifacts={artifacts}
            selected={selectedArtifact}
            detail={artifactDetail}
            pending={pending}
            projectId={projectId}
            category={null}
            label="Attempt artifacts"
            onSelect={setSelectedArtifact}
          />
        ) : <Empty title="No run artifacts yet">This attempt has not allocated a run.</Empty> : null}
      </div>
      <details className="attempt-technical">
        <summary>Technical identity</summary>
        <div className="attempt-technical-grid">
          <OverviewValue label="Run" value={experiment.run_id} mono />
          <OverviewValue label="Base snapshot" value={experiment.base_snapshot_id} mono />
          <OverviewValue label="Candidate snapshot" value={experiment.candidate_snapshot_id} mono />
        </div>
      </details>
    </aside>
  );
}

function Stat({
  label,
  value,
  tone,
  total,
}: {
  label: string;
  value: number;
  tone: "runs" | "pass" | "fail" | "rep";
  total?: number;
}) {
  const percent = total ? Math.round((value / total) * 100) : null;
  return (
    <div className={`stat ${tone}`}>
      <span className="k">{label}</span>
      <span className="v">
        {value}
        {percent !== null && value > 0 ? <small>{percent}%</small> : null}
      </span>
    </div>
  );
}

function SplitHandle({
  value,
  onChange,
}: {
  value: number;
  onChange: (value: number) => void;
}) {
  function startDrag(event: React.MouseEvent<HTMLDivElement>) {
    const container = event.currentTarget.closest(".work");
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const move = (moveEvent: MouseEvent) => {
      const next = ((moveEvent.clientX - rect.left) / rect.width) * 100;
      onChange(Math.min(74, Math.max(30, next)));
    };
    const done = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", done);
      document.body.style.userSelect = "";
    };
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", done);
  }
  return (
    <div
      className="handle"
      role="separator"
      aria-label="Resize panel"
      tabIndex={0}
      aria-valuenow={Math.round(value)}
      aria-valuemin={30}
      aria-valuemax={74}
      onMouseDown={startDrag}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") onChange(Math.max(30, value - 3));
        if (event.key === "ArrowRight") onChange(Math.min(74, value + 3));
      }}
    >
      <span className="grip" />
    </div>
  );
}

function RunLedger({
  runs,
  selected,
  loading,
  active,
  project,
  onOpen,
  onTrigger,
  onCopy,
}: {
  runs: Run[];
  selected: string;
  loading: string;
  active: boolean;
  project: Project | null;
  onOpen: (run: Run, tab?: StandardTab) => void;
  onTrigger: (run: Run) => void;
  onCopy: (runId: string) => void;
}) {
  if (!project) {
    return (
      <div className="empty">
        <div className="big">No project selected</div>
        <div>Spin up a workspace, then open it here.</div>
        <div className="cmd"><b>autoexp init</b> demo_eval &nbsp;·&nbsp; <b>autoexp view</b></div>
      </div>
    );
  }
  if (!runs.length) {
    return (
      <div className="empty">
        <div className="big">No runs yet</div>
        <div>Run the experiment to create the first traceable run.</div>
        <div className="cmd"><b>autoexp run</b></div>
      </div>
    );
  }
  return (
    <div className="ledger-wrap">
      <table className="ledger">
        <thead>
          <tr>
            <th style={{ width: "22%" }}>Title</th>
            <th style={{ width: "19%" }}>Run</th>
            <th style={{ width: "12%" }}>Changed</th>
            <th style={{ width: "10%" }}>Status</th>
            <th style={{ width: "13%" }}>Script</th>
            <th style={{ width: "10%" }}>Report</th>
            <th style={{ width: "9%" }}>Output</th>
            <th style={{ width: "5%" }}>Re-run</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.run_id}
              className={selected === run.run_id ? "sel" : ""}
              tabIndex={0}
              aria-label={`Inspect run ${run.run_id}`}
              onClick={() => onOpen(run)}
              onKeyDown={(event) => {
                if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
                event.preventDefault();
                onOpen(run);
              }}
            >
              <td>
                <span className="run-title" title={run.title}>{run.title || "Untitled run"}</span>
              </td>
              <td>
                <div className="runid">
                  <div className="runid-stack">
                    <span className="id">{run.run_id}</span>
                    <span className="when">{relativeTime(run.created_at)}</span>
                  </div>
                  <button
                    className="copy"
                    title="Copy run ID"
                    onClick={(event) => {
                      event.stopPropagation();
                      onCopy(run.run_id);
                    }}
                  >
                    <Clipboard size={13} />
                  </button>
                </div>
              </td>
              <td>
                <div className="change-tags">
                  {(run.changes || ["unknown"]).map((change) => (
                    <span className={change === "output drift" ? "warn" : ""} key={change}>{change}</span>
                  ))}
                </div>
              </td>
              <td>
                <span className={`status ${run.status}`}>
                  <span className="d" />
                  {statusLabel(run.status)}
                </span>
              </td>
              <td>
                <button
                  className="cell-btn"
                  onClick={(event) => {
                    event.stopPropagation();
                    onOpen(run, "script");
                  }}
                >
                  <Code2 size={14} />
                  <span className="t">
                    {run.script_name || run.script || "script"}
                  </span>
                </button>
              </td>
              <td>
                {run.report_path ? (
                  <button
                    className="cell-btn"
                    onClick={(event) => {
                      event.stopPropagation();
                      onOpen(run, "report");
                    }}
                  >
                    <FileText size={14} />
                    <span className="t">view report</span>
                  </button>
                ) : (
                  <span className="dash">—</span>
                )}
              </td>
              <td>
                {run.output_files?.length ? (
                  <button
                    className="out"
                    title={run.output_files.join("\n")}
                    onClick={(event) => {
                      event.stopPropagation();
                      onOpen(run, "output");
                    }}
                  >
                    {run.output_files[0].split("/").pop()}
                    {run.output_files.length > 1 ? <span className="more"> +{run.output_files.length - 1}</span> : null}
                  </button>
                ) : (
                  <span className="dash">—</span>
                )}
              </td>
              <td>
                <button
                  className="trigger"
                  disabled={active || loading === `trigger:${run.run_id}`}
                  title="Re-run this snapshot"
                  onClick={(event) => {
                    event.stopPropagation();
                    onTrigger(run);
                  }}
                >
                  <Play size={13} fill="currentColor" />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const STANDARD_TABS: { id: StandardTab; label: string; icon: LucideIcon }[] = [
  { id: "overview", label: "Overview", icon: FlaskConical },
  { id: "script", label: "Script", icon: Code2 },
  { id: "output", label: "Output", icon: FolderOpen },
  { id: "logs", label: "Logs", icon: Terminal },
  { id: "report", label: "Report", icon: FileText },
  { id: "diff", label: "Changes", icon: GitBranch },
];

function projectUrl(path: string, projectId: string) {
  if (!projectId || /^(data:|blob:)/.test(path)) return path;
  const url = new URL(path, window.location.origin);
  if (!url.searchParams.has("project_id")) url.searchParams.set("project_id", projectId);
  return /^https?:/.test(path) ? url.toString() : `${url.pathname}${url.search}${url.hash}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "not recorded";
  if (typeof value === "boolean") return value ? "verified" : "not verified";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(valueText).join(", ");
  const record = asRecord(value);
  if (!record) return String(value);
  for (const key of ["status", "state", "value", "detail", "message", "version"]) {
    if (record[key] !== undefined) return valueText(record[key]);
  }
  return Object.entries(record).map(([key, item]) => `${key}: ${valueText(item)}`).join(" · ");
}

function humanize(value: string) {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatBytes(value?: number | null) {
  if (value === null || value === undefined) return "size unknown";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

function normalizeRunDetail(payload: RunDetailPayload, fallback: Run): RunAggregate {
  if ("run" in payload) return { ...payload, run: { ...fallback, ...payload.run } };
  return {
    run: { ...fallback, ...payload },
    source_snapshot: payload.source_snapshot,
    parent_run: payload.parent_run,
    trigger: payload.trigger,
    reproducibility: payload.reproducibility,
    artifact_summary: payload.artifact_summary,
    reproduction: payload.reproduction,
    divergence: payload.divergence,
  };
}

function StandardInspector({
  run,
  tab,
  theme,
  projectId,
  rerunning,
  runningSnapshot,
  onTab,
  onRerun,
  onRunSnapshot,
  onSaveScript,
  onClose,
}: {
  run: Run;
  tab: StandardTab;
  theme: Theme;
  projectId: string;
  rerunning: boolean;
  runningSnapshot: boolean;
  onTab: (tab: StandardTab) => void;
  onRerun: (run: Run) => Promise<void>;
  onRunSnapshot: (snapshotId: string) => Promise<void>;
  onSaveScript: (
    runId: string,
    snapshotId: string | null,
    path: string,
    text: string,
    saveAs: string,
  ) => Promise<ScriptSavePayload>;
  onClose: () => void;
}) {
  const [overview, setOverview] = useState<RunAggregate | null>(null);
  const [source, setSource] = useState<SourcePayload | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [artifactDetail, setArtifactDetail] = useState<ArtifactDetailPayload | null>(null);
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [diff, setDiff] = useState<RunDiffPayload | null>(null);
  const [derivedSnapshot, setDerivedSnapshot] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [paneError, setPaneError] = useState("");
  const endpoint = useCallback(
    (suffix: string) => projectUrl(`/api/runs/${encodeURIComponent(run.run_id)}${suffix}`, projectId),
    [projectId, run.run_id],
  );

  useEffect(() => {
    setOverview(null);
    setSource(null);
    setArtifacts([]);
    setSelectedArtifact(null);
    setArtifactDetail(null);
    setReport(null);
    setDiff(null);
    setDerivedSnapshot(null);
    setPaneError("");
  }, [run.run_id]);

  useEffect(() => {
    let live = true;
    let timer = 0;
    const pull = async () => {
      try {
        const data = normalizeRunDetail(await api<RunDetailPayload>(endpoint("")), run);
        if (!live) return;
        setOverview(data);
        if (!["success", "failed", "canceled"].includes(data.run.status)) {
          timer = window.setTimeout(pull, 1000);
        }
      } catch (caught) {
        if (live) setPaneError(caught instanceof Error ? caught.message : String(caught));
      }
    };
    void pull();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [endpoint, run.run_id]);

  useEffect(() => {
    if (tab === "overview" || tab === "logs") return;
    let live = true;
    setPending(true);
    setPaneError("");
    const request =
      tab === "script"
        ? api<SourcePayload>(endpoint("/source")).then((data) => { if (live) setSource(data); })
        : tab === "output"
          ? api<ArtifactListPayload>(endpoint("/artifacts")).then((data) => {
              if (!live) return;
              const items = Array.isArray(data) ? data : data.artifacts || [];
              setArtifacts(items);
              setSelectedArtifact(items.find((item) => !item.category || item.category === "output") || null);
            })
          : tab === "report"
            ? api<ReportPayload | { report: ReportPayload }>(endpoint("/report")).then((data) => {
                if (live) setReport("report" in data ? data.report : data);
              })
            : api<RunDiffPayload>(endpoint("/diff")).then((data) => { if (live) setDiff(data); });
    request.catch((caught) => {
      if (live) setPaneError(caught instanceof Error ? caught.message : String(caught));
    }).finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [endpoint, overview?.run.status, tab]);

  useEffect(() => {
    if (!selectedArtifact) {
      setArtifactDetail(null);
      return;
    }
    let live = true;
    setArtifactDetail(null);
    setPending(true);
    api<ArtifactDetailPayload>(endpoint(`/artifacts/${encodeURIComponent(selectedArtifact.artifact_id)}`))
      .then((data) => { if (live) setArtifactDetail(data); })
      .catch((caught) => {
        if (live) setPaneError(caught instanceof Error ? caught.message : String(caught));
      })
      .finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [endpoint, selectedArtifact]);

  return (
    <aside className="card panel run-inspector">
      <div className="run-inspector-head">
        <div className="inspector-title">
          <span className="ic"><FlaskConical size={17} /></span>
          <div>
            <div className="inspector-id">{run.run_id}</div>
            <div className="inspector-sub">{statusLabel(overview?.run.status || run.status)} · immutable execution evidence</div>
          </div>
        </div>
        <div className="inspector-actions">
          {derivedSnapshot ? (
            <button className="btn primary sm" onClick={() => onRunSnapshot(derivedSnapshot)} disabled={runningSnapshot}>
              <Play size={13} fill="currentColor" /> {runningSnapshot ? "Starting…" : "Run snapshot"}
            </button>
          ) : null}
          <button className="btn sm" onClick={() => onRerun(run)} disabled={rerunning}>
            <Repeat2 size={14} /> {rerunning ? "Starting…" : "Re-run"}
          </button>
          <button className="btn ghost sm" onClick={onClose} aria-label="Close inspector"><X size={16} /></button>
        </div>
      </div>
      <div className="inspector-tabs" role="tablist" aria-label="Run inspector">
        {STANDARD_TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            role="tab"
            aria-selected={tab === id}
            onClick={() => onTab(id)}
          >
            <Icon size={13} /> {label}
          </button>
        ))}
      </div>
      <div className={`panel-body run-pane${tab === "overview" ? " scroll-pane" : ""}`}>
        {paneError ? <div className="pane-error"><AlertTriangle size={14} /> {paneError}</div> : null}
        {tab === "overview" ? (
          overview ? <RunOverview data={overview} onNavigate={onTab} /> : <PaneLoading />
        ) : null}
        {tab === "script" ? (
          source ? (
            <SourceEditor
              data={source}
              onSave={async (snapshotId, path, text, saveAs) => {
                const result = await onSaveScript(run.run_id, snapshotId, path, text, saveAs);
                setDerivedSnapshot(result.snapshot.snapshot_id);
                return result;
              }}
            />
          ) : pending ? <PaneLoading /> : !paneError ? <Empty title="No source">This run has no pinned source.</Empty> : null
        ) : null}
        {tab === "output" ? (
          <ArtifactViewer
            artifacts={artifacts}
            selected={selectedArtifact}
            detail={artifactDetail}
            pending={pending}
            projectId={projectId}
            onSelect={setSelectedArtifact}
          />
        ) : null}
        {tab === "logs" ? <RunLogs run={overview?.run || run} projectId={projectId} /> : null}
        {tab === "report" ? (
          report ? <RunReport data={report} /> : pending ? <PaneLoading /> : !paneError ? <Empty title="No report yet">No report is attached to this run.</Empty> : null
        ) : null}
        {tab === "diff" ? (
          diff ? <RunDiff data={diff} /> : pending ? <PaneLoading /> : !paneError ? <Empty title="No source changes">This run matches its comparison snapshot.</Empty> : null
        ) : null}
      </div>
    </aside>
  );
}

function PaneLoading() {
  return <div className="empty"><div className="big">Loading evidence…</div></div>;
}

function RunOverview({ data, onNavigate }: { data: RunAggregate; onNavigate: (tab: StandardTab) => void }) {
  const run = data.run;
  const snapshot = data.source_snapshot || run.source_snapshot;
  const parent = data.parent_run || run.parent_run;
  const trigger = data.trigger || run.trigger;
  const reproducibility = data.reproducibility || run.reproducibility;
  const artifactSummary = data.artifact_summary || run.artifact_summary;
  const triggerText = typeof trigger === "string"
    ? trigger
    : trigger
      ? [trigger.kind, trigger.actor_name || trigger.actor].filter(Boolean).join(" · ")
      : run.trigger_id || "not recorded";
  const duration = run.duration_ms === null || run.duration_ms === undefined
    ? "not recorded"
    : run.duration_ms < 1000
      ? `${run.duration_ms} ms`
      : `${(run.duration_ms / 1000).toFixed(2)} s`;
  const reproductionRecord = asRecord(data.reproduction || run.reproduction);
  const comparisonRun = valueText(reproductionRecord?.run_id);
  const reproduction = run.reproduces_run_id
    ? `reproduces ${run.reproduces_run_id}`
    : data.divergence || run.divergence || reproductionRecord?.state === "divergence"
      ? `Different output from ${comparisonRun}: the recorded source, parameters, runtime, and declared inputs match. This usually means nondeterministic behavior or an undeclared external input.`
      : reproductionRecord?.state === "reproduction"
        ? `reproduces ${valueText(reproductionRecord.run_id)}`
        : valueText(data.reproduction || run.reproduction);
  const checks = reproducibility
    ? Object.entries(reproducibility.checks || reproducibility).filter(([key]) => key !== "state")
    : [];
  const externalInputs = Array.isArray(reproducibility?.external_inputs)
    ? reproducibility.external_inputs.map(asRecord).filter(Boolean) as Record<string, unknown>[]
    : [];
  const artifacts = artifactSummary ? Object.entries(artifactSummary).filter(([, value]) => typeof value === "number") : [];

  return (
    <div className="overview-grid">
      <section className="evidence-section result-evidence">
        <h3>Results &amp; artifacts</h3>
        <div className="artifact-summary">
          {artifacts.length ? artifacts.map(([label, value]) => {
            const target = ({ output: "output", log: "logs", report: "report" } as const)[label as "output" | "log" | "report"];
            return target ? (
              <button key={label} onClick={() => onNavigate(target)}><b>{String(value)}</b>{humanize(label)}</button>
            ) : (
              <span key={label}><b>{String(value)}</b>{humanize(label)}</span>
            );
          }) : <span>No artifact summary recorded.</span>}
        </div>
        <div className={`reproduction-note${/diverg/i.test(reproduction) ? " warn" : ""}`}>{reproduction}</div>
        {run.failure_message ? (
          <div className="failure-note"><b>{run.failure_kind || "execution failure"}</b>{run.failure_message}</div>
        ) : null}
      </section>

      <section className="evidence-section">
        <h3>Reproducibility</h3>
        <div className="check-list">
          {checks.length ? checks.map(([label, value]) => {
            const text = valueText(value);
            const warning = /not |unpin|redact|warn|missing|unknown/i.test(text);
            return (
              <div className={`check-row${warning ? " warn" : ""}`} key={label}>
                <span className="check-icon">{warning ? "!" : "✓"}</span>
                <span><b>{humanize(label)}</b><small>{text}</small></span>
              </div>
            );
          }) : <div className="evidence-empty">No reproducibility summary recorded.</div>}
          {externalInputs.map((input) => (
            <div className={`check-row${input.reproducibility_state === "pinned" ? "" : " warn"}`} key={String(input.name)}>
              <span className="check-icon">{input.reproducibility_state === "pinned" ? "✓" : "!"}</span>
              <span>
                <b>{String(input.name)}</b>
                <small>{valueText(input.kind)} · {valueText(input.reproducibility_state)} · {input.present ? "present" : "missing"}</small>
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="evidence-section">
        <h3>Run identity</h3>
        <div className="kv-grid">
          <OverviewValue label="Source snapshot" value={snapshot?.snapshot_id || run.source_snapshot_id} />
          <OverviewValue label="Trigger" value={triggerText} />
          <OverviewValue label="Capsule hash" value={run.capsule_hash} mono />
          <OverviewValue label="Output hash" value={run.output_hash} mono />
          <OverviewValue label="Runner" value={[run.runner, run.runner_identity].filter(Boolean).join(" · ")} />
          <OverviewValue label="Duration / exit" value={`${duration} · exit ${run.exit_code ?? "—"}`} />
          <OverviewValue label="Started" value={run.started_at} />
          <OverviewValue label="Ended" value={run.ended_at} />
        </div>
      </section>

      <section className="evidence-section">
        <h3>Lineage</h3>
        <div className="lineage-row">
          <div className="lineage-node"><span>parent run</span><b>{parent?.run_id || run.parent_run_id || "root"}</b></div>
          <span className="lineage-arrow">→</span>
          <div className="lineage-node"><span>source snapshot</span><b>{snapshot?.snapshot_id || run.source_snapshot_id || "unknown"}</b></div>
          <span className="lineage-arrow">→</span>
          <div className="lineage-node current"><span>current run</span><b>{run.run_id}</b></div>
        </div>
      </section>

    </div>
  );
}

function OverviewValue({ label, value, mono = false }: { label: string; value: unknown; mono?: boolean }) {
  return <div className="kv"><span>{label}</span><b className={mono ? "mono-inline" : ""}>{valueText(value)}</b></div>;
}

function ArtifactViewer({
  artifacts,
  selected,
  detail,
  pending,
  projectId,
  category = "output",
  label = "Output artifacts",
  onSelect,
}: {
  artifacts: Artifact[];
  selected: Artifact | null;
  detail: ArtifactDetailPayload | null;
  pending: boolean;
  projectId: string;
  category?: string | null;
  label?: string;
  onSelect: (artifact: Artifact) => void;
}) {
  const visible = category ? artifacts.filter((item) => !item.category || item.category === category) : artifacts;
  if (!visible.length && pending) return <PaneLoading />;
  if (!visible.length) return <Empty title="No artifacts">This run did not produce indexed artifacts.</Empty>;
  return (
    <div className="artifact-layout">
      <div className="artifact-list">
        <div className="list-label">{label} · {visible.length}</div>
        {visible.map((artifact) => (
          <button
            key={artifact.artifact_id}
            className={selected?.artifact_id === artifact.artifact_id ? "active" : ""}
            onClick={() => onSelect(artifact)}
          >
            <FileText size={14} />
            <span><b>{artifact.path.split("/").pop()}</b><small>{artifact.media_type || "unknown"} · {formatBytes(artifact.size_bytes)}</small></span>
          </button>
        ))}
      </div>
      <div className="artifact-preview">
        {selected ? (
          <div className="preview-head">
            <span>{selected.path}</span>
            <small>
              {selected.media_type || "unknown"} · {formatBytes(selected.size_bytes)}
              {detail?.content_url || selected.content_url ? (
                <> · <a href={projectUrl(detail?.content_url || selected.content_url!, projectId)} target="_blank" rel="noreferrer">raw</a></>
              ) : null}
            </small>
          </div>
        ) : null}
        {detail ? <ArtifactPreview artifact={selected!} detail={detail} projectId={projectId} /> : <PaneLoading />}
      </div>
    </div>
  );
}

function ArtifactPreview({ artifact, detail, projectId }: { artifact: Artifact; detail: ArtifactDetailPayload; projectId: string }) {
  const preview = asRecord(detail.preview);
  const media = detail.artifact?.media_type || artifact.media_type || "";
  const imageUrl = detail.data_url || detail.content_url || valueText(preview?.data_url || preview?.content_url);
  const text = detail.text ?? detail.content ?? (typeof preview?.text === "string" ? preview.text : undefined) ?? (typeof detail.preview === "string" ? detail.preview : undefined);
  const columns = detail.columns || (Array.isArray(preview?.columns) ? preview.columns.map(String) : undefined);
  const rows = detail.rows || (Array.isArray(preview?.rows) ? preview.rows as unknown[][] : undefined);
  const json = detail.json ?? preview?.json ?? preview?.value;

  if (media.startsWith("image/") && imageUrl !== "not recorded") {
    return <div className="image-preview"><img src={projectUrl(imageUrl, projectId)} alt={artifact.path} /></div>;
  }
  if (columns?.length && rows) {
    return (
      <div className="table-preview">
        <table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
          <tbody>{rows.map((row, index) => (
            <tr key={index}>{columns.map((column, cell) => <td key={column}>{valueText(Array.isArray(row) ? row[cell] : (row as Record<string, unknown>)[column])}</td>)}</tr>
          ))}</tbody>
        </table>
        {detail.truncated || preview?.truncated ? <div className="preview-note">Bounded preview · additional rows omitted.</div> : null}
      </div>
    );
  }
  if (json !== undefined || (
    detail.preview && typeof detail.preview === "object" && !preview?.rows && preview?.kind !== "binary"
  )) {
    return <pre className="raw-preview">{JSON.stringify(json ?? detail.preview, null, 2)}</pre>;
  }
  if (text !== undefined) return <pre className="raw-preview">{text}</pre>;
  if (preview?.kind === "binary") {
    return (
      <div className="binary-preview">
        <FileText size={28} />
        <b>Preview unavailable</b>
        <span>{media || "binary file"} · {formatBytes(artifact.size_bytes)}</span>
        {artifact.metadata ? <pre>{JSON.stringify(artifact.metadata, null, 2)}</pre> : null}
      </div>
    );
  }
  return (
    <div className="binary-preview">
      <FileText size={28} />
      <b>Preview unavailable</b>
      <span>{media || "binary file"} · {formatBytes(artifact.size_bytes)}</span>
      {detail.metadata ? <pre>{JSON.stringify(detail.metadata, null, 2)}</pre> : null}
    </div>
  );
}

function RunLogs({ run, projectId }: { run: Run; projectId: string }) {
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const [buffers, setBuffers] = useState({ stdout: "", stderr: "" });
  const [states, setStates] = useState({ stdout: false, stderr: false });
  const [error, setError] = useState("");
  const offsets = useRef({ stdout: 0, stderr: 0 });
  const terminal = useRef({ stdout: false, stderr: false });
  const viewRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    offsets.current = { stdout: 0, stderr: 0 };
    terminal.current = { stdout: false, stderr: false };
    setBuffers({ stdout: "", stderr: "" });
    setStates({ stdout: false, stderr: false });
    setError("");
  }, [run.run_id]);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    const pull = async () => {
      try {
        const offset = offsets.current[stream];
        const data = await api<RunLogPayload>(
          projectUrl(`/api/runs/${encodeURIComponent(run.run_id)}/logs/${stream}?offset=${offset}`, projectId),
        );
        if (stopped) return;
        if (data.text) setBuffers((current) => ({ ...current, [stream]: current[stream] + data.text }));
        offsets.current[stream] = data.next_offset;
        terminal.current[stream] = data.terminal;
        setStates((current) => ({ ...current, [stream]: data.terminal }));
        setError("");
        if (!data.terminal) timer = window.setTimeout(pull, 1000);
      } catch (caught) {
        if (!stopped) setError(caught instanceof Error ? caught.message : String(caught));
      }
    };
    if (!terminal.current[stream]) void pull();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [projectId, run.run_id, stream]);

  useEffect(() => {
    if (viewRef.current) viewRef.current.scrollTop = viewRef.current.scrollHeight;
  }, [buffers, stream]);

  return (
    <div className="log run-log">
      <div className="logbar">
        <div className="stream-tabs">
          {(["stdout", "stderr"] as const).map((item) => (
            <button key={item} className={stream === item ? "active" : ""} onClick={() => setStream(item)}>{item}</button>
          ))}
        </div>
        <span className={states[stream] ? "idle" : "live"}><span className="d" />{states[stream] ? "terminal" : "polling by byte offset"}</span>
      </div>
      {error ? <div className="pane-error"><AlertTriangle size={14} /> {error}</div> : null}
      <div className="logview" ref={viewRef}>
        {buffers[stream] || <span className="dim">waiting for {stream}…</span>}
        {!states[stream] ? <span className="cursor" /> : null}
      </div>
    </div>
  );
}

function RunReport({ data, project = false }: { data: Pick<ReportPayload, "path" | "text">; project?: boolean }) {
  const [raw, setRaw] = useState(false);
  const text = normalizeMarkdown(data.text);
  return (
    <div className="editor-shell">
      <div className="ptools">
        <span className="tool-label">{data.path || "report"}</span>
        <div className="right"><button className="btn sm" onClick={() => setRaw(!raw)}>{raw ? "Rendered" : "Raw"}</button></div>
      </div>
      {raw ? <pre className="raw-preview report-raw">{text}</pre> : text ? <MarkdownViewer path="" text={text} /> : (
        <Empty title={project ? "No project report yet" : "No report yet"}>
          {project ? "Ask the connected agent to synthesize project_summary and write the final project report." : "No report is attached to this run."}
        </Empty>
      )}
    </div>
  );
}

function RunDiff({ data }: { data: RunDiffPayload }) {
  const text = data.unified_diff || data.diff || "";
  const summary = Array.isArray(data.summary)
    ? data.summary.map((item, index) => [`change ${index + 1}`, item] as const)
    : Object.entries(data.summary || data.changes || {});
  return (
    <div className="editor-shell">
      <div className="diff-summary">
        {summary.map(([label, value]) => (
          <span key={label}>
            <b>{humanize(label)}</b>
            {typeof value === "boolean" ? (value ? "changed" : "unchanged") : valueText(value)}
          </span>
        ))}
        {data.changed_files?.map((file) => <span key={file}><b>File</b>{file}</span>)}
        {!summary.length && !data.changed_files?.length ? <span>Semantic summary unavailable.</span> : null}
      </div>
      <UnifiedDiff text={text} />
    </div>
  );
}

function UnifiedDiff({ text }: { text: string }) {
  if (!text) return <Empty title="No source changes">The selected snapshot matches its parent.</Empty>;
  let oldLine: number | null = null;
  let newLine: number | null = null;
  return (
    <div className="diff">
      {text.split("\n").map((line, index) => {
        const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        let oldNumber: number | null = null;
        let newNumber: number | null = null;
        if (hunk) {
          oldLine = Number(hunk[1]);
          newLine = Number(hunk[2]);
        } else if (oldLine !== null && newLine !== null && !line.startsWith("\\")) {
          if (line.startsWith("+") && !line.startsWith("+++")) newNumber = newLine++;
          else if (line.startsWith("-") && !line.startsWith("---")) oldNumber = oldLine++;
          else if (!line.startsWith("---") && !line.startsWith("+++")) {
            oldNumber = oldLine++;
            newNumber = newLine++;
          }
        }
        if (hunk || /^(diff --git |index |--- |\+\+\+ )/.test(line)) return null;
        const kind = hunk || line.startsWith("---") || line.startsWith("+++")
          ? "hunk"
          : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
        return (
          <div className={`dl ${kind}`} key={index}>
            <span className="diff-ln">{oldNumber ?? ""}</span>
            <span className="diff-ln">{newNumber ?? ""}</span>
            <span className="diff-code">{line || " "}</span>
          </div>
        );
      })}
    </div>
  );
}

function Viewer({
  panel,
  projectId,
  active,
  onSaveInstruction,
  onSaveParams,
  onClose,
}: {
  panel: Panel;
  projectId: string;
  active: boolean;
  onSaveInstruction: (text: string) => Promise<InstructionPayload>;
  onSaveParams: (params: Record<string, unknown>) => Promise<ParamsPayload>;
  onClose: () => void;
}) {
  const title =
    panel.kind === "instruction"
      ? "Report instruction"
      : panel.kind === "project-report"
        ? "Project report"
      : panel.kind === "params"
        ? "Parameters"
        : "Worker log";
  const Icon =
    panel.kind === "instruction" || panel.kind === "project-report"
      ? FileText
      : panel.kind === "log"
        ? Terminal
        : panel.kind === "params"
          ? SlidersHorizontal
          : Code2;

  return (
    <aside className="card panel">
      <div className="card-head">
        <div className="ptitle">
          <span className="ic"><Icon size={16} /></span>
          <span className="txt">{title}</span>
        </div>
        <button className="btn ghost sm" onClick={onClose} aria-label="Close panel">
          <X size={16} />
        </button>
      </div>
      <div className="panel-body">
        {panel.kind === "project-report" ? <RunReport data={panel.data} project /> : null}
        {panel.kind === "instruction" ? (
          <TextEditor
            value={panel.data.text}
            label={panel.data.source}
            onSave={onSaveInstruction}
          />
        ) : null}
        {panel.kind === "params" ? (
          <ParamsEditor data={panel.data} onSave={onSaveParams} />
        ) : null}
        {panel.kind === "log" ? (
          <LogViewer projectId={projectId} active={active} kind="worker" />
        ) : null}
      </div>
    </aside>
  );
}

function CodePane({
  value,
  editing,
  onChange,
  path,
}: {
  value: string;
  editing: boolean;
  onChange: (value: string) => void;
  path?: string;
}) {
  if (path && !editing) {
    const extension = path.split(".").pop()?.toLowerCase();
    const language = extension === "py" ? "python"
      : extension === "json" ? "json"
        : extension === "md" ? "markdown"
          : extension === "toml" ? "ini"
            : "plaintext";
    const highlighted = language === "plaintext"
      ? value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      : hljs.highlight(value, { language }).value;
    const lines = value.split("\n");
    return (
      <div className="code">
        <div className="codeflex">
          <div className="gutter">
            {lines.map((_, index) => <div key={index}>{index + 1}</div>)}
          </div>
          <div className="codearea">
            <pre><code dangerouslySetInnerHTML={{ __html: highlighted }} /></pre>
          </div>
        </div>
      </div>
    );
  }
  const lines = value.split("\n");
  return (
    <div className="code">
      <div className="codeflex">
        <div className="gutter">
          {lines.map((_, index) => <div key={index}>{index + 1}</div>)}
        </div>
        <div className="codearea">
          {editing ? (
            <textarea
              spellCheck={false}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Tab") return;
                event.preventDefault();
                const target = event.currentTarget;
                const start = target.selectionStart;
                const end = target.selectionEnd;
                onChange(value.slice(0, start) + "    " + value.slice(end));
                requestAnimationFrame(() => {
                  target.selectionStart = target.selectionEnd = start + 4;
                });
              }}
              style={{ height: `${lines.length * 20.625 + 28}px` }}
            />
          ) : (
            <pre><code>{value}</code></pre>
          )}
        </div>
      </div>
    </div>
  );
}

function SourceEditor({
  data,
  onSave,
}: {
  data: SourcePayload;
  onSave: (
    snapshotId: string | null,
    path: string,
    text: string,
    saveAs: string,
  ) => Promise<ScriptSavePayload>;
}) {
  const [files, setFiles] = useState(data.files);
  const [selected, setSelected] = useState(data.selected);
  const [draft, setDraft] = useState("");
  const [saveAs, setSaveAs] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [baseSnapshotId, setBaseSnapshotId] = useState<string | null>(null);
  const file = useMemo(
    () => files.find((item) => item.path === selected) || files[0],
    [files, selected],
  );

  useEffect(() => {
    setFiles(data.files);
    setSelected(data.selected);
    setBaseSnapshotId(null);
    setEditing(false);
  }, [data]);
  useEffect(() => {
    setDraft(file?.text || "");
    setSaveAs(file ? nextScriptPath(file.path, files) : "");
    setEditing(false);
  }, [file?.path, files]);

  if (!file) {
    return <Empty title="No script snapshot">This run has no source files to show.</Empty>;
  }
  const changed = draft !== file.text;

  async function save() {
    if (!changed) return;
    setSaving(true);
    try {
      const result = await onSave(
        baseSnapshotId,
        file.path,
        draft,
        saveAs || nextScriptPath(file.path, files),
      );
      setBaseSnapshotId(result.snapshot.snapshot_id);
      setFiles((current) => {
        const derived = result.path === file.path
          ? current
          : current.filter((item) => item.path !== file.path);
        return derived.some((item) => item.path === result.path)
          ? derived.map((item) =>
              item.path === result.path ? { ...item, text: draft } : item,
            )
          : [...derived, { path: result.path, text: draft }];
      });
      setSelected(result.path);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="editor-shell">
      <div className="ptools">
        <select value={file.path} onChange={(event) => setSelected(event.target.value)}>
          {files.map((item) => (
            <option key={item.path} value={item.path}>{item.path}</option>
          ))}
        </select>
        {editing ? (
          <input
            className="saveas"
            value={saveAs}
            onChange={(event) => setSaveAs(event.target.value)}
            aria-label="Save as new snapshot"
          />
        ) : null}
        <div className="right">
          {editing && changed ? <span className="savehint">saves a new snapshot</span> : null}
          {!editing ? (
            <button className="btn sm" onClick={() => setEditing(true)}>
              <Pencil size={14} /> Edit
            </button>
          ) : (
            <button className="btn primary sm" onClick={save} disabled={!changed || saving}>
              <Save size={14} /> {saving ? "Saving…" : "Save snapshot"}
            </button>
          )}
        </div>
      </div>
      <CodePane
        value={editing ? draft : file.text}
        editing={editing}
        onChange={setDraft}
        path={file.path}
      />
    </div>
  );
}

function ParamsEditor({
  data,
  onSave,
}: {
  data: ParamsPayload;
  onSave: (params: Record<string, unknown>) => Promise<ParamsPayload>;
}) {
  const initial = JSON.stringify(data.params || {}, null, 2);
  const [draft, setDraft] = useState(initial);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [invalid, setInvalid] = useState(false);

  useEffect(() => {
    setDraft(JSON.stringify(data.params || {}, null, 2));
    setEditing(false);
    setInvalid(false);
  }, [data]);
  const changed = draft !== initial;

  function update(value: string) {
    setDraft(value);
    try {
      JSON.parse(value);
      setInvalid(false);
    } catch {
      setInvalid(true);
    }
  }
  async function save() {
    if (!changed || invalid) return;
    setSaving(true);
    try {
      await onSave(JSON.parse(draft) as Record<string, unknown>);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="editor-shell">
      <div className="ptools">
        <span className="tool-label">Experiment parameters · applied on the next run</span>
        <div className="right">
          {editing && invalid ? <span className="savehint invalid">invalid JSON</span> : null}
          {!editing ? (
            <button className="btn sm" onClick={() => setEditing(true)}>
              <Pencil size={14} /> Edit
            </button>
          ) : (
            <button
              className="btn primary sm"
              onClick={save}
              disabled={!changed || invalid || saving}
            >
              <Save size={14} /> {saving ? "Saving…" : "Save"}
            </button>
          )}
        </div>
      </div>
      <CodePane value={draft} editing={editing} onChange={update} />
    </div>
  );
}

function TextEditor({
  value,
  label,
  onSave,
}: {
  value: string;
  label: string;
  onSave: (text: string) => Promise<unknown>;
}) {
  const [draft, setDraft] = useState(value);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setDraft(value);
    setEditing(false);
  }, [value]);
  const changed = draft !== value;

  async function save() {
    if (!changed) return;
    setSaving(true);
    try {
      await onSave(draft);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="editor-shell">
      <div className="ptools">
        <span className="tool-label">{label}</span>
        <div className="right">
          {!editing ? (
            <button className="btn sm" onClick={() => setEditing(true)}>
              <Pencil size={14} /> Edit
            </button>
          ) : (
            <button className="btn primary sm" onClick={save} disabled={!changed || saving}>
              <Save size={14} /> {saving ? "Saving…" : "Save"}
            </button>
          )}
        </div>
      </div>
      <CodePane value={draft} editing={editing} onChange={setDraft} />
    </div>
  );
}

function MarkdownViewer({ path, text }: { path: string; text: string }) {
  if (!text) {
    return (
      <Empty title="No report yet">
        Write a report under this run&apos;s <span className="mono-inline">report/</span> directory,
        or let an agent generate one.
      </Empty>
    );
  }
  return (
    <div className="md">
      {path ? <div className="mpath">{path}</div> : null}
      <div className="mbody">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
      </div>
    </div>
  );
}

function LogViewer({
  projectId,
  active,
  kind,
}: {
  projectId: string;
  active: boolean;
  kind: "worker" | "agent";
}) {
  const [log, setLog] = useState("");
  const viewRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let mounted = true;
    const pull = async () => {
      try {
        const data = await api<{ log: string }>(
          `/api/run/log?tail_bytes=65536&project_id=${encodeURIComponent(projectId)}`,
        );
        if (mounted) setLog(data.log || "");
      } catch {
        // Status polling reports server errors in the main banner.
      }
    };
    void pull();
    const timer = window.setInterval(pull, 1000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, [projectId]);

  useEffect(() => {
    if (viewRef.current) viewRef.current.scrollTop = viewRef.current.scrollHeight;
  }, [log]);

  return (
    <div className="log">
      <div className="logbar">
        {active ? (
          <span className="live"><span className="d" />active {kind} process · outer process log</span>
        ) : (
          <span className="idle">no active {kind} process · showing its last outer log</span>
        )}
        <span className="log-project">{projectId}</span>
      </div>
      <div className="logview" ref={viewRef}>
        {log ? (
          log.split("\n").map((line, index) => {
            const className = /exit 0|finished|wrote/.test(line)
              ? "ok"
              : /error|fail|\^C|canceled|traceback/i.test(line)
                ? "err"
                : /^\$|^resolved|^starting|^loaded/.test(line)
                  ? "dim"
                  : "ln";
            return <div key={index} className={className}>{line || " "}</div>;
          })
        ) : (
          <div className="dim">waiting for output…</div>
        )}
        {active ? <span className="cursor" /> : null}
      </div>
    </div>
  );
}

function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <div className="big">{title}</div>
      <div>{children}</div>
    </div>
  );
}
