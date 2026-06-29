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
  X,
} from "lucide-react";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "@/api";
import type {
  InstructionPayload,
  ParamsPayload,
  Project,
  ReportPayload,
  ResearchExperiment,
  ResearchFilePayload,
  ResearchObjective,
  ResearchState,
  Run,
  SourcePayload,
} from "@/types";

type Theme = "light" | "dark";
type ProjectMode = "standard" | "autoresearch";
type Panel =
  | { kind: "script"; run: Run; data: SourcePayload }
  | { kind: "report"; run: Run; data: ReportPayload }
  | { kind: "instruction"; data: InstructionPayload }
  | { kind: "params"; data: ParamsPayload }
  | { kind: "program"; data: ResearchFilePayload }
  | { kind: "evaluator"; data: ResearchFilePayload }
  | { kind: "diff"; experiment: ResearchExperiment; objective: ResearchObjective }
  | { kind: "log" };
type JobState = { active: boolean; job: { status?: string } | null };
type Toast = { id: number; message: string; kind?: "ok" | "bad" };

const EMPTY_RESEARCH: ResearchState = {
  objective: { metric: "score", direction: "max", baseline: null, best: null, budget_sec: 300 },
  files: [
    { path: "script/program.md", role: "human", desc: "research directions and loop rules" },
    { path: "script/train.py", role: "agent", desc: "the implementation the agent improves" },
    { path: "script/evaluate.py", role: "frozen", desc: "the stable evaluator" },
  ],
  experiments: [],
  loop: { active: false, phase: "idle", tag: null },
};

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

  const project = projects.find((item) => item.project_id === projectId) || null;
  const isResearch = project?.mode === "autoresearch";
  const researchView = projectMode === "autoresearch";
  const filteredProjects = projects.filter((item) =>
    projectMode === "autoresearch" ? item.mode === "autoresearch" : item.mode !== "autoresearch",
  );
  const hasAvailableProjects = filteredProjects.some((item) => item.exists);
  const displayedResearch = researchView ? (project ? research : EMPTY_RESEARCH) : null;
  const active = isResearch ? Boolean(research?.loop.active) : job.active;

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
  }

  async function loadResearch(id = projectId) {
    if (!id) return;
    setResearch(await api<ResearchState>(projectPath("/api/research", id)));
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
        if (project?.exists && !active) {
          if (isResearch) void startResearchLoop();
          else void startRun();
        }
      } else if (event.key === "Escape" && panel) {
        setPanel(null);
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [project, active, isResearch, panel]);

  async function startRun(run?: Run) {
    if (!projectId) return;
    if (run) setSelectedRun(run.run_id);
    setLoading(run ? `trigger:${run.run_id}` : "start");
    setError("");
    try {
      const data = await api<JobState>("/api/run/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          ...(run ? { run_id: run.run_id } : {}),
        }),
      });
      setJob(data);
      await loadStatus(projectId);
      toast(run ? `Re-running ${run.run_id}` : "Run started");
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      toast("Couldn't start the run", "bad");
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

  async function openScript(run: Run) {
    setSelectedRun(run.run_id);
    setLoading(`script:${run.run_id}`);
    setError("");
    try {
      const data = await api<SourcePayload>(
        projectPath(`/api/run/source?run_id=${encodeURIComponent(run.run_id)}`),
      );
      setPanel({ kind: "script", run, data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function openReport(run: Run) {
    setSelectedRun(run.run_id);
    setLoading(`report:${run.run_id}`);
    setError("");
    try {
      const data = await api<ReportPayload>(
        projectPath(`/api/run/report?run_id=${encodeURIComponent(run.run_id)}`),
      );
      setPanel({ kind: "report", run, data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function openInstruction() {
    if (!projectId) return;
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
    path: string,
    text: string,
    saveAs: string,
  ) {
    const result = await api<{ path: string; run: Run }>("/api/script/file", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId,
        run_id: runId,
        path,
        text,
        save_as: saveAs,
      }),
    });
    setSelectedRun(result.run.run_id);
    await loadStatus(projectId);
    toast(`Saved ${saveAs}`, "ok");
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
    toast("Params saved", "ok");
    return data;
  }

  async function startResearchLoop() {
    if (!projectId) return;
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
      setPanel(data.role === "human" ? { kind: "program", data } : { kind: "evaluator", data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function openResearchDiff(experiment: ResearchExperiment) {
    setSelectedRun(experiment.tag);
    setLoading(`research-diff:${experiment.tag}`);
    setError("");
    try {
      const data = await api<{ tag: string; diff: string }>(
        projectPath(`/api/research/diff?tag=${encodeURIComponent(experiment.tag)}`),
      );
      setPanel({ kind: "diff", experiment: { ...experiment, diff: data.diff }, objective: research!.objective });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading("");
    }
  }

  async function saveProgram(text: string) {
    const data = await api<ResearchFilePayload>("/api/research/program", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, text }),
    });
    setPanel({ kind: "program", data });
    toast("Program saved", "ok");
    return data;
  }

  const filteredRuns = runs.filter((run) => {
    const text =
      `${run.run_id} ${run.status} ${run.script_name || ""} ${run.report_path || ""} ${(run.output_files || []).join(" ")}`.toLowerCase();
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
            <span className={`lamp${active ? " live" : ""}`}>
              <span className="dot" />
              {active
                ? isResearch
                  ? `loop ${research?.loop.phase || "running"}`
                  : `run ${job.job?.status || "running"}`
                : "idle"}
            </span>
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

        {project && !isResearch ? (
          <section className="band">
            <div className="meta">
              <span className="title">{project.title}</span>
              <span className="path">{project.path}</span>
            </div>
            <span className="runner">runner · {project.runner || "local"}</span>
            <div className="actions">
              {job.active ? (
                <button className="btn danger" onClick={stopRun} disabled={loading === "stop"}>
                  <Square size={15} fill="currentColor" /> Stop run
                </button>
              ) : (
                <button
                  className="btn primary"
                  onClick={() => startRun()}
                  disabled={loading === "start" || !project.exists}
                  title="Run the current script with current params · ⌘↵"
                >
                  <Play size={14} fill="currentColor" /> Run
                </button>
              )}
              <button className="btn" onClick={() => setPanel({ kind: "log" })}>
                <Terminal size={15} /> Live log
              </button>
              <button className="btn" onClick={openParams} disabled={!project.exists}>
                <SlidersHorizontal size={15} /> Params
              </button>
              <button className="btn" onClick={openInstruction} disabled={!project.exists}>
                <FileText size={15} /> Report instruction
              </button>
            </div>
          </section>
        ) : null}

        {project && isResearch && research ? (
          <section className="band">
            <div className="meta">
              <span className="title">{project.title}</span>
              <span className="path">{project.path}</span>
            </div>
            <div className="research-chips">
              <span className="chip">
                <Target size={14} /> {research.objective.metric}
                <span className="dir">{research.objective.direction === "min" ? "↓ min" : "↑ max"}</span>
              </span>
              <span className="chip best">
                <FlaskConical size={13} /> best <b>{formatScore(research.objective.best)}</b>
                <span className="from">/ baseline {formatScore(research.objective.baseline)}</span>
              </span>
              <span className="chip"><Repeat2 size={13} /> {Math.round(research.objective.budget_sec / 60)} min/exp</span>
            </div>
            <div className="actions">
              {research.loop.active ? (
                <button className="btn danger" onClick={stopResearchLoop} disabled={loading === "research-loop"}>
                  <Square size={15} fill="currentColor" /> Stop loop
                </button>
              ) : (
                <button className="btn primary" onClick={startResearchLoop} disabled={loading === "research-loop"}>
                  <Repeat2 size={15} /> Start loop
                </button>
              )}
              <button className="btn" onClick={() => openResearchFile("script/program.md")}>
                <FileText size={15} /> program.md
              </button>
              <button className="btn" onClick={() => setPanel({ kind: "log" })}>
                <Terminal size={15} /> Live log
              </button>
            </div>
          </section>
        ) : null}

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
            onExperiment={openResearchDiff}
            onOpenFile={openResearchFile}
            onResize={setLeftColumn}
            onSaveProgram={saveProgram}
            onClose={() => setPanel(null)}
          />
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
              <div>
                <div className="eyebrow">ledger</div>
                <h2>Recent runs</h2>
              </div>
              {project ? <span className="sub">Each run pins the script and config that produced it</span> : null}
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
              onSelect={setSelectedRun}
              onScript={openScript}
              onReport={openReport}
              onTrigger={startRun}
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
          {panel ? (
            <Viewer
              panel={panel}
              theme={theme}
              projectId={projectId}
              active={job.active}
              onSaveScript={saveScript}
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
  onSaveProgram,
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
  onSaveProgram: (text: string) => Promise<ResearchFilePayload>;
  onClose: () => void;
}) {
  const kept = research.experiments.filter((item) => item.status === "kept").length;
  const reverted = research.experiments.filter((item) => item.status === "reverted").length;
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

      <div className="card">
        <div className="card-head">
          <div>
            <div className="eyebrow">contract</div>
            <h2>Files &amp; ownership</h2>
          </div>
          <span className="sub">The human directs, the agent edits, and the evaluator stays fixed</span>
        </div>
        <ResearchContract files={research.files} onOpen={onOpenFile} disabled={preview} />
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
                ? `loop running · ${research.loop.phase}`
                : "loop idle"}
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
            onSaveProgram={onSaveProgram}
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
  const values = scored.map((item) => item.score);
  const low = Math.min(...values, baseline, best);
  const high = Math.max(...values, baseline, best);
  const padding = (high - low) * 0.18 || Math.max(Math.abs(high) * 0.02, 0.01);
  const min = low - padding;
  const max = high + padding;
  const width = 720;
  const height = 210;
  const left = 56;
  const right = 18;
  const top = 14;
  const bottom = 28;
  const x = (index: number) =>
    left +
    (chronological.length <= 1
      ? 0
      : (index / (chronological.length - 1)) * (width - left - right));
  const y = (value: number) =>
    top + ((max - value) / (max - min)) * (height - top - bottom);

  let currentBest: number | null = objective.baseline;
  const step: [number, number][] = [];
  chronological.forEach((item, index) => {
    if (item.score !== null && item.status === "kept") {
      currentBest =
        currentBest === null
          ? item.score
          : objective.direction === "min"
            ? Math.min(currentBest, item.score)
            : Math.max(currentBest, item.score);
    }
    if (currentBest !== null) step.push([x(index), y(currentBest)]);
  });
  const path = step
    .map((point, index) =>
      index === 0
        ? `M ${point[0]} ${point[1]}`
        : `L ${point[0]} ${step[index - 1][1]} L ${point[0]} ${point[1]}`,
    )
    .join(" ");
  const ticks = [max, (max + min) / 2, min];
  const lastStep = step[step.length - 1];

  return (
    <div className="chart-wrap">
      <svg className="ratchet" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Objective score and best-so-far by attempt">
        {ticks.map((tick) => (
          <g key={tick}>
            <line className="gl" x1={left} y1={y(tick)} x2={width - right} y2={y(tick)} />
            <text className="tick" x={left - 8} y={y(tick) + 3} textAnchor="end">{formatScore(tick)}</text>
          </g>
        ))}
        <line className="base" x1={left} y1={y(baseline)} x2={width - right} y2={y(baseline)} />
        <text className="tick" x={width - right} y={y(baseline) - 5} textAnchor="end">
          baseline {formatScore(baseline)}
        </text>
        <path className="step" d={path} />
        {chronological.map((item, index) =>
          item.score === null ? null : (
            <circle
              key={item.tag}
              className={item.status === "kept" ? "kept" : "rev"}
              cx={x(index)}
              cy={y(item.score)}
              r={item.status === "kept" ? 3.6 : 3.2}
            />
          ),
        )}
        {lastStep ? (
          <text className="blabel" x={lastStep[0]} y={lastStep[1] - 7} textAnchor="end">
            best {formatScore(best)}
          </text>
        ) : null}
        <text className="tick" x={left} y={height - 8}>a01</text>
        <text className="tick" x={width - right} y={height - 8} textAnchor="end">attempt →</text>
      </svg>
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
        <div className="crow" key={file.path}>
          <span className={`role ${file.role}`}>
            {file.role === "frozen" ? <Lock size={11} /> : file.role === "agent" ? <Bot size={12} /> : null}
            {file.role}
          </span>
          <div className="contract-name">
            <div className="fname">{file.path}</div>
            <div className="fdesc">{file.desc}</div>
          </div>
          <div className="fmeta">
            {file.hash ? (
              <span className="fhash"><Lock size={12} /> {file.hash}</span>
            ) : null}
            <button className="btn sm" onClick={() => onOpen(file.path)} disabled={disabled}>
              {file.role === "human" ? <Pencil size={13} /> : file.role === "frozen" ? <Lock size={13} /> : <Code2 size={13} />}
              {file.role === "human" ? "Edit" : "Open"}
            </button>
          </div>
        </div>
      ))}
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
  const deltas = new Map<string, number>();
  let best = objective.baseline;
  [...experiments].reverse().forEach((item) => {
    if (item.score === null || item.status !== "kept") return;
    if (best !== null) {
      deltas.set(
        item.tag,
        objective.direction === "min" ? best - item.score : item.score - best,
      );
    }
    best =
      best === null
        ? item.score
        : objective.direction === "min"
          ? Math.min(best, item.score)
          : Math.max(best, item.score);
  });
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
            const delta = deltas.get(item.tag);
            return (
              <tr
                key={item.tag}
                className={`${item.status === "reverted" ? "rev-row " : ""}${selected === item.tag ? "sel" : ""}`}
                onClick={() => {
                  onSelect(item.tag);
                  onOpen(item);
                }}
              >
                <td><span className="tagcell">{item.tag}</span></td>
                <td><span className={`status ${item.status}`}><span className="d" />{item.status}</span></td>
                <td>{item.score === null ? <span className="dash">—</span> : <span className="score">{formatScore(item.score)}</span>}</td>
                <td>{delta === undefined ? <span className="dash">—</span> : <span className={`delta ${delta > 0 ? "good" : "flat"}`}>{delta > 0 ? "+" : ""}{formatScore(delta)}</span>}</td>
                <td><span className="hyp">{item.hyp}</span></td>
                <td>
                  {item.commit ? (
                    <span className="commitcell"><GitCommit size={13} /> {item.commit}</span>
                  ) : item.status === "running" ? (
                    <span className="commitcell"><GitBranch size={13} /> autoexp/{item.tag}</span>
                  ) : (
                    <span className="dash">reverted</span>
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
  onSaveProgram,
  onClose,
}: {
  panel: Panel;
  theme: Theme;
  projectId: string;
  active: boolean;
  onSaveProgram: (text: string) => Promise<ResearchFilePayload>;
  onClose: () => void;
}) {
  const title =
    panel.kind === "program"
      ? `${panel.data.path} · research directions`
      : panel.kind === "evaluator"
        ? `${panel.data.path} · ${panel.data.role}`
        : panel.kind === "diff"
          ? `${panel.experiment.tag} · experiment diff`
          : "Live log";
  const Icon = panel.kind === "program" ? FileText : panel.kind === "evaluator" ? Lock : panel.kind === "diff" ? GitBranch : Terminal;
  return (
    <aside className="card panel">
      <div className="card-head">
        <div className="ptitle"><span className="ic"><Icon size={16} /></span><span className="txt">{title}</span></div>
        <button className="btn ghost sm" onClick={onClose} aria-label="Close panel"><X size={16} /></button>
      </div>
      <div className="panel-body">
        {panel.kind === "program" ? (
          <TextEditor value={panel.data.text} label={panel.data.path} onSave={onSaveProgram} />
        ) : null}
        {panel.kind === "evaluator" ? <ResearchFileViewer data={panel.data} /> : null}
        {panel.kind === "diff" ? <DiffViewer experiment={panel.experiment} objective={panel.objective} /> : null}
        {panel.kind === "log" ? <LogViewer projectId={projectId} active={active} /> : null}
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

function DiffViewer({
  experiment,
  objective,
}: {
  experiment: ResearchExperiment;
  objective: ResearchObjective;
}) {
  const lines = (experiment.diff || "").split("\n");
  let oldLine: number | null = null;
  let newLine: number | null = null;
  const numbered = lines.map((line, sourceIndex) => {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    let oldNumber: number | null = null;
    let newNumber: number | null = null;
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
    } else if (oldLine !== null && newLine !== null && !line.startsWith("\\")) {
      if (line.startsWith("+")) newNumber = newLine++;
      else if (line.startsWith("-")) oldNumber = oldLine++;
      else {
        oldNumber = oldLine++;
        newNumber = newLine++;
      }
    }
    return { line, oldNumber, newNumber, sourceIndex };
  });
  const changed = numbered.filter(({ line }) =>
    (line.startsWith("+") && !line.startsWith("+++")) ||
    (line.startsWith("-") && !line.startsWith("---")),
  );
  return (
    <div className="editor-shell">
      <div className="diffhead">
        <div className="dt">
          {experiment.tag} · {experiment.status}
          {experiment.score === null ? "" : ` · ${objective.metric} ${formatScore(experiment.score)}`}
        </div>
        <div className="dh">{experiment.hyp}</div>
      </div>
      <div className="diff">
        {changed.length ? changed.map(({ line, oldNumber, newNumber, sourceIndex }, index) => {
          const className = line.startsWith("+") ? "add" : "del";
          const separated = index > 0 && sourceIndex > changed[index - 1].sourceIndex + 1;
          return <React.Fragment key={sourceIndex}>
            {separated ? <div className="diff-gap" /> : null}
            <div className={`dl ${className}`}>
              <span className="diff-ln">{oldNumber ?? ""}</span>
              <span className="diff-ln">{newNumber ?? ""}</span>
              <span className="diff-code">{line || " "}</span>
            </div>
          </React.Fragment>;
        }) : <div className="diff-empty">No source changes in this attempt.</div>}
      </div>
    </div>
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
  onSelect,
  onScript,
  onReport,
  onTrigger,
  onCopy,
}: {
  runs: Run[];
  selected: string;
  loading: string;
  active: boolean;
  project: Project | null;
  onSelect: (runId: string) => void;
  onScript: (run: Run) => void;
  onReport: (run: Run) => void;
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
            <th style={{ width: "26%" }}>Run</th>
            <th style={{ width: "13%" }}>Status</th>
            <th style={{ width: "21%" }}>Script</th>
            <th style={{ width: "16%" }}>Report</th>
            <th style={{ width: "16%" }}>Output</th>
            <th style={{ width: "8%" }}>Re-run</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.run_id}
              className={selected === run.run_id ? "sel" : ""}
              onClick={() => onSelect(run.run_id)}
            >
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
                    onScript(run);
                  }}
                >
                  <Code2 size={14} />
                  <span className="t">
                    {loading === `script:${run.run_id}`
                      ? "loading…"
                      : run.script_name || run.script || "script"}
                  </span>
                </button>
              </td>
              <td>
                {run.report_path ? (
                  <button
                    className="cell-btn"
                    onClick={(event) => {
                      event.stopPropagation();
                      onReport(run);
                    }}
                  >
                    <FileText size={14} />
                    <span className="t">
                      {loading === `report:${run.run_id}` ? "loading…" : "view report"}
                    </span>
                  </button>
                ) : (
                  <span className="dash">—</span>
                )}
              </td>
              <td>
                {run.output_files?.length ? (
                  <span className="out" title={run.output_files.join("\n")}>
                    {run.output_files[0].split("/").pop()}
                    {run.output_files.length > 1 ? (
                      <span className="more"> +{run.output_files.length - 1}</span>
                    ) : null}
                  </span>
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

function Viewer({
  panel,
  theme,
  projectId,
  active,
  onSaveScript,
  onSaveInstruction,
  onSaveParams,
  onClose,
}: {
  panel: Panel;
  theme: Theme;
  projectId: string;
  active: boolean;
  onSaveScript: (
    runId: string,
    path: string,
    text: string,
    saveAs: string,
  ) => Promise<{ path: string; run: Run }>;
  onSaveInstruction: (text: string) => Promise<InstructionPayload>;
  onSaveParams: (params: Record<string, unknown>) => Promise<ParamsPayload>;
  onClose: () => void;
}) {
  const title =
    panel.kind === "script"
      ? <><span className="muted">{panel.run.run_id}</span> / script</>
      : panel.kind === "report"
        ? <><span className="muted">{panel.run.run_id}</span> / report</>
        : panel.kind === "instruction"
          ? "Report instruction"
          : panel.kind === "params"
            ? "Params · script/params.json"
            : "Live log";
  const Icon =
    panel.kind === "report" || panel.kind === "instruction"
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
        {panel.kind === "script" ? (
          <SourceEditor
            data={panel.data}
            theme={theme}
            onSave={(path, text, saveAs) =>
              onSaveScript(panel.run.run_id, path, text, saveAs)
            }
          />
        ) : null}
        {panel.kind === "report" ? (
          <MarkdownViewer path={panel.data.path} text={panel.data.text} />
        ) : null}
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
          <LogViewer projectId={projectId} active={active} />
        ) : null}
      </div>
    </aside>
  );
}

function CodePane({
  value,
  editing,
  onChange,
}: {
  value: string;
  editing: boolean;
  onChange: (value: string) => void;
}) {
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
  theme: Theme;
  onSave: (
    path: string,
    text: string,
    saveAs: string,
  ) => Promise<{ path: string; run: Run }>;
}) {
  const [files, setFiles] = useState(data.files);
  const [selected, setSelected] = useState(data.selected);
  const [draft, setDraft] = useState("");
  const [saveAs, setSaveAs] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const file = useMemo(
    () => files.find((item) => item.path === selected) || files[0],
    [files, selected],
  );

  useEffect(() => {
    setFiles(data.files);
    setSelected(data.selected);
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
        file.path,
        draft,
        saveAs || nextScriptPath(file.path, files),
      );
      setFiles((current) =>
        current.some((item) => item.path === result.path)
          ? current.map((item) =>
              item.path === result.path ? { ...item, text: draft } : item,
            )
          : [...current, { path: result.path, text: draft }],
      );
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
      <CodePane value={editing ? draft : file.text} editing={editing} onChange={setDraft} />
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
        <span className="tool-label">script/params.json · applied on the next run</span>
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
}: {
  projectId: string;
  active: boolean;
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
          <span className="live"><span className="d" />active run · streaming stdout</span>
        ) : (
          <span className="idle">no active run · showing the last job&apos;s output</span>
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
