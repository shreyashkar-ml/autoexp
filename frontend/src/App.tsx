import {
  AlertTriangle,
  Check,
  ChevronDown,
  Clipboard,
  Code2,
  FileText,
  FolderOpen,
  Moon,
  Pencil,
  Play,
  RefreshCw,
  Save,
  Search,
  SlidersHorizontal,
  Square,
  Sun,
  Terminal,
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
  Run,
  SourcePayload,
} from "@/types";

type Theme = "light" | "dark";
type Panel =
  | { kind: "script"; run: Run; data: SourcePayload }
  | { kind: "report"; run: Run; data: ReportPayload }
  | { kind: "instruction"; data: InstructionPayload }
  | { kind: "params"; data: ParamsPayload }
  | { kind: "log" };
type JobState = { active: boolean; job: { status?: string } | null };
type Toast = { id: number; message: string; kind?: "ok" | "bad" };

function statusLabel(status: string) {
  return status === "success" ? "completed" : status || "unknown";
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
  const [runs, setRuns] = useState<Run[]>([]);
  const [job, setJob] = useState<JobState>({ active: false, job: null });
  const [query, setQuery] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [panel, setPanel] = useState<Panel | null>(null);
  const [leftColumn, setLeftColumn] = useState(52);
  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const searchRef = useRef<HTMLInputElement>(null);

  const project = projects.find((item) => item.project_id === projectId) || null;

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
    setProjects(data.projects || []);
    const next =
      data.selected_project_id ||
      data.projects.find((item) => item.exists)?.project_id ||
      "";
    setProjectId((current) => preferred || current || next);
    return preferred || next;
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

  async function refresh() {
    setError("");
    try {
      const id = await loadProjects(projectId);
      await loadStatus(id);
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
    loadStatus(projectId).catch((caught) =>
      setError(caught instanceof Error ? caught.message : String(caught)),
    );
    const timer = window.setInterval(() => {
      loadStatus(projectId).catch(() => undefined);
    }, 1400);
    return () => window.clearInterval(timer);
  }, [projectId]);

  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if (event.key === "/" && document.activeElement !== searchRef.current) {
        event.preventDefault();
        searchRef.current?.focus();
      } else if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        if (project?.exists && !job.active) void startRun();
      } else if (event.key === "Escape" && panel) {
        setPanel(null);
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [project, job.active, panel]);

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
            <b>auto<i>exp</i></b>
            <span className="tag">local autonomous experimentation</span>
          </div>
          <div className="bar-spacer" />
          <div className="bar-tools">
            <label className="picker">
              <FolderOpen size={15} />
              <span className="lbl">project</span>
              <select
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
                disabled={!projects.length}
              >
                {!projects.length ? <option value="">No projects</option> : null}
                {projects.map((item) => (
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
            <span className={`lamp${job.active ? " live" : ""}`}>
              <span className="dot" />
              {job.active ? `run ${job.job?.status || "running"}` : "idle"}
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

        {project ? (
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

        {error ? (
          <div className="err-banner">
            <AlertTriangle size={15} /> {error}
          </div>
        ) : null}

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
              <span className="sub">
                {project
                  ? "Each run pins the script and config that produced it"
                  : "Create or register an autoexp project to begin"}
              </span>
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
  onSave: (text: string) => Promise<InstructionPayload>;
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
