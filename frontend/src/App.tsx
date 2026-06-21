import { Edit3, FileCode2, FileText, FolderOpen, Moon, Play, RefreshCw, Save, Search, Square, Sun, X } from "lucide-react";
import React, { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import Editor from "@monaco-editor/react";
import remarkGfm from "remark-gfm";

import { api } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { InstructionPayload, Project, ReportPayload, Run, SourcePayload } from "@/types";

type Panel =
  | { kind: "script"; run: Run; data: SourcePayload }
  | { kind: "report"; run: Run; data: ReportPayload }
  | { kind: "instruction"; data: InstructionPayload };

type JobState = { active: boolean; job: { status?: string } | null };

const EDITOR_OPTIONS = {
  automaticLayout: true,
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  wordWrap: "on",
  fontSize: 13,
  lineNumbersMinChars: 3,
  overviewRulerBorder: false,
  renderLineHighlight: "none",
} as const;

function statusLabel(status: string) {
  return status === "success" ? "completed" : status || "unknown";
}

function editorLanguage(path: string) {
  const ext = path.split(".").pop();
  return {
    css: "css",
    html: "html",
    js: "javascript",
    jsx: "javascript",
    json: "json",
    md: "markdown",
    py: "python",
    sh: "shell",
    ts: "typescript",
    tsx: "typescript",
    yaml: "yaml",
    yml: "yaml",
  }[ext || ""] || "text";
}

function nextScriptPath(path: string, files: { path: string }[]) {
  const parts = path.split("/");
  const name = parts.pop() || "script.py";
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const suffix = dot > 0 ? name.slice(dot) : "";
  const base = stem.replace(/_v\d+$/, "");
  let highest = 1;

  for (const file of files) {
    const fileName = file.path.split("/").pop() || "";
    const fileDot = fileName.lastIndexOf(".");
    const fileStem = fileDot > 0 ? fileName.slice(0, fileDot) : fileName;
    const fileSuffix = fileDot > 0 ? fileName.slice(fileDot) : "";
    if (fileSuffix !== suffix) continue;
    if (fileStem === base) highest = Math.max(highest, 1);
    const match = fileStem.match(new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}_v(\\d+)$`));
    if (match) highest = Math.max(highest, Number(match[1]));
  }

  return [...parts, `${base}_v${highest + 1}${suffix}`].filter(Boolean).join("/");
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [split, setSplit] = useState(50);
  const [selectedRun, setSelectedRun] = useState("");
  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [activeJob, setActiveJob] = useState<JobState>({ active: false, job: null });
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    if (typeof window === "undefined") return "light";
    return window.localStorage.getItem("autoexp-theme") === "dark" ? "dark" : "light";
  });

  const selectedProject = projects.find((project) => project.project_id === selectedProjectId) || null;

  function projectPath(path: string) {
    const joiner = path.includes("?") ? "&" : "?";
    return `${path}${joiner}project_id=${encodeURIComponent(selectedProjectId)}`;
  }

  async function loadProjects(preferredId = selectedProjectId) {
    const suffix = preferredId ? `?project_id=${encodeURIComponent(preferredId)}` : "";
    const data = await api<{ projects: Project[]; selected_project_id: string | null }>(`/api/projects${suffix}`);
    setProjects(data.projects || []);
    const next = data.selected_project_id || data.projects.find((project) => project.exists)?.project_id || "";
    setSelectedProjectId((current) => preferredId || current || next);
    return preferredId || next;
  }

  async function loadRuns() {
    if (!selectedProjectId) return;
    const data = await api<{ runs: Run[] }>(projectPath("/api/runs?limit=100"));
    setRuns(data.runs || []);
  }

  async function loadStatus() {
    if (!selectedProjectId) {
      setActiveJob({ active: false, job: null });
      setRuns([]);
      return;
    }
    const data = await api<{ run: JobState; runs: Run[] }>(projectPath("/api/status?limit=100"));
    setActiveJob(data.run);
    setRuns(data.runs || []);
  }

  async function refresh() {
    setError("");
    try {
      await loadProjects(selectedProjectId);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    loadProjects().catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem("autoexp-theme", theme);
  }, [theme]);

  useEffect(() => {
    setPanel(null);
    setSelectedRun("");
    setQuery("");
    loadStatus().catch((err) => setError(err instanceof Error ? err.message : String(err)));
    const timer = window.setInterval(() => {
      loadStatus().catch((err) => setError(err instanceof Error ? err.message : String(err)));
    }, 2500);
    return () => window.clearInterval(timer);
  }, [selectedProjectId]);

  async function startRun(run?: Run) {
    if (!selectedProjectId) return;
    if (run) setSelectedRun(run.run_id);
    setLoading(run ? `trigger:${run.run_id}` : "start");
    setError("");
    try {
      const data = await api<JobState>("/api/run/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: selectedProjectId, ...(run ? { run_id: run.run_id } : {}) }),
      });
      setActiveJob(data);
      await loadRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading("");
    }
  }

  async function stopRun() {
    if (!selectedProjectId) return;
    setLoading("stop");
    setError("");
    try {
      const data = await api<JobState>("/api/run/kill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: selectedProjectId }),
      });
      setActiveJob(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading("");
    }
  }

  async function openScript(run: Run) {
    setSelectedRun(run.run_id);
    setLoading(`script:${run.run_id}`);
    setError("");
    try {
      const data = await api<SourcePayload>(projectPath(`/api/run/source?run_id=${encodeURIComponent(run.run_id)}`));
      setPanel({ kind: "script", run, data });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading("");
    }
  }

  async function openReport(run: Run) {
    setSelectedRun(run.run_id);
    setLoading(`report:${run.run_id}`);
    setError("");
    try {
      const data = await api<ReportPayload>(projectPath(`/api/run/report?run_id=${encodeURIComponent(run.run_id)}`));
      setPanel({ kind: "report", run, data });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading("");
    }
  }

  async function openInstruction() {
    if (!selectedProjectId) return;
    setLoading("instruction");
    setError("");
    try {
      const data = await api<InstructionPayload>(projectPath("/api/report/instruction"));
      setPanel({ kind: "instruction", data });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading("");
    }
  }

  async function saveScriptFile(runId: string, path: string, text: string, saveAs: string) {
    const result = await api<{ path: string; run: Run }>("/api/script/file", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: selectedProjectId, run_id: runId, path, text, save_as: saveAs }),
    });
    setSelectedRun(result.run.run_id);
    await loadStatus();
    return result;
  }

  async function saveInstruction(text: string) {
    const data = await api<InstructionPayload>("/api/report/instruction", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: selectedProjectId, text }),
    });
    setPanel({ kind: "instruction", data });
    return data;
  }

  const filteredRuns = runs.filter((run) => {
    const haystack = `${run.run_id} ${run.status} ${run.script_name || ""} ${run.report_path || ""} ${(run.output_files || []).join(" ")}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });

  const metrics = {
    total: runs.length,
    completed: runs.filter((run) => run.status === "success").length,
    failed: runs.filter((run) => run.status === "failed").length,
    reports: runs.filter((run) => run.report_path).length,
  };

  return (
    <main className="app-shell">
      <div className="page">
        <header className="global-header">
          <div className="brand-logo">
            <img src="/autoexp.png" alt="Autoexp - Local Autonomous Experimentation" />
          </div>

          <div className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-2">
            <label className="project-picker">
              <FolderOpen className="h-4 w-4 text-muted-foreground" />
              <span className="text-xs font-medium uppercase text-muted-foreground">Project</span>
              <select
                className="project-select"
                value={selectedProjectId}
                onChange={(event) => setSelectedProjectId(event.target.value)}
                disabled={!projects.length}
              >
                {!projects.length ? <option value="">No projects</option> : null}
                {projects.map((project) => (
                  <option key={project.project_id} value={project.project_id} disabled={!project.exists}>
                    {project.title}
                  </option>
                ))}
              </select>
            </label>

            <Badge className={activeJob.active ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground"}>
              {activeJob.active ? `run ${activeJob.job?.status || "running"}` : "idle"}
            </Badge>
            <Button variant="outline" size="icon" onClick={refresh} disabled={loading === "start" || loading === "stop"} aria-label="Refresh">
              <RefreshCw className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              aria-label={theme === "dark" ? "Use light mode" : "Use dark mode"}
            >
              {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
          </div>
        </header>

        {selectedProject ? (
          <section className="project-band">
            <div className="min-w-0">
              <div className="text-sm font-semibold text-foreground">{selectedProject.title}</div>
              <div className="mono mt-1 truncate text-xs text-muted-foreground">{selectedProject.path}</div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {activeJob.active ? (
                <Button onClick={stopRun} disabled={loading === "stop"}>
                  <Square className="h-4 w-4" />
                  Stop
                </Button>
              ) : (
                <Button onClick={() => startRun()} disabled={loading === "start" || !selectedProject.exists}>
                  <Play className="h-4 w-4" />
                  Run
                </Button>
              )}
              <Button variant="outline" onClick={openInstruction} disabled={!selectedProject.exists}>
                <FileText className="h-4 w-4" />
                Report Instruction
              </Button>
            </div>
          </section>
        ) : null}

        {error ? (
          <div className="rounded-md border border-destructive bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        <section className="grid gap-3 md:grid-cols-4">
          <Metric label="Runs" value={metrics.total} />
          <Metric label="Completed" value={metrics.completed} tone="success" />
          <Metric label="Failed" value={metrics.failed} tone="danger" />
          <Metric label="Reports" value={metrics.reports} />
        </section>

        <section
          className={panel ? "workspace-split" : "grid min-h-0 flex-1"}
          style={panel ? { gridTemplateColumns: `${split}% 6px minmax(0, 1fr)` } : undefined}
        >
          <div className="surface flex min-h-[520px] min-w-0 flex-col overflow-hidden">
            <div className="flex flex-col gap-3 border-b p-4 md:flex-row md:items-center md:justify-between">
              <div>
                <h2 className="text-base font-semibold">Recent runs</h2>
                <p className="text-sm text-muted-foreground">{selectedProject ? "Project artifacts and reports" : "Create or register an Autoexp project to begin"}</p>
              </div>
              <label className="relative w-full md:w-72">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <input
                  className="h-9 w-full rounded-md border bg-card pl-9 pr-3 text-sm outline-none focus:ring-2 focus:ring-ring"
                  placeholder="Filter runs"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  disabled={!selectedProject}
                />
              </label>
            </div>
            <RunsTable
              runs={filteredRuns}
              selectedRun={selectedRun}
              loading={loading}
              active={activeJob.active}
              onSelect={setSelectedRun}
              onScript={openScript}
              onReport={openReport}
              onTrigger={startRun}
            />
          </div>
          {panel ? <SplitHandle value={split} onChange={setSplit} /> : null}
          {panel ? <Viewer panel={panel} theme={theme} onSaveScript={saveScriptFile} onSaveInstruction={saveInstruction} onClose={() => setPanel(null)} /> : null}
        </section>
      </div>
    </main>
  );
}

function SplitHandle({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  function startDrag(event: React.MouseEvent<HTMLButtonElement>) {
    const container = event.currentTarget.parentElement;
    if (!container) return;

    const rect = container.getBoundingClientRect();
    const move = (moveEvent: MouseEvent) => {
      const next = ((moveEvent.clientX - rect.left) / rect.width) * 100;
      onChange(Math.min(72, Math.max(28, next)));
    };
    const done = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", done);
    };

    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", done);
  }

  return (
    <button
      className="split-handle"
      type="button"
      aria-label="Resize viewer split"
      aria-valuemin={28}
      aria-valuemax={72}
      aria-valuenow={Math.round(value)}
      onMouseDown={startDrag}
    />
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone?: "success" | "danger" }) {
  return (
    <div className="metric">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className={tone === "success" ? "mt-2 text-2xl font-semibold text-accent" : tone === "danger" ? "mt-2 text-2xl font-semibold text-destructive" : "mt-2 text-2xl font-semibold text-primary"}>
        {value}
      </div>
    </div>
  );
}

function RunsTable({
  runs,
  selectedRun,
  loading,
  active,
  onSelect,
  onScript,
  onReport,
  onTrigger,
}: {
  runs: Run[];
  selectedRun: string;
  loading: string;
  active: boolean;
  onSelect: (runId: string) => void;
  onScript: (run: Run) => void;
  onReport: (run: Run) => void;
  onTrigger: (run: Run) => void;
}) {
  return (
    <div className="table-scroll flex-1 overflow-auto">
      <Table className="min-w-[980px] table-fixed">
        <TableHeader>
          <TableRow>
            <TableHead className="w-[22%] text-left">Run ID</TableHead>
            <TableHead className="w-[14%]">Status</TableHead>
            <TableHead className="w-[22%]">Script</TableHead>
            <TableHead className="w-[18%]">Report</TableHead>
            <TableHead className="w-[12%]">Output</TableHead>
            <TableHead className="w-[12%]">Trigger</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => (
            <TableRow
              key={run.run_id}
              className={selectedRun === run.run_id ? "bg-muted/60" : ""}
              onClick={() => onSelect(run.run_id)}
            >
              <TableCell className="mono truncate text-left text-xs">{run.run_id}</TableCell>
              <TableCell>
                <span className={run.status === "failed" ? "font-semibold text-destructive" : "font-semibold text-accent"}>
                  {statusLabel(run.status)}
                </span>
              </TableCell>
              <TableCell>
                <Button
                  variant="outline"
                  size="sm"
                  className="max-w-full justify-start"
                  onClick={(event) => {
                    event.stopPropagation();
                    onScript(run);
                  }}
                >
                  <FileCode2 className="h-4 w-4 shrink-0" />
                  <span className="truncate">{loading === `script:${run.run_id}` ? "loading" : run.script_name || run.script || `script-${run.run_id}`}</span>
                </Button>
              </TableCell>
              <TableCell>
                {run.report_path ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={(event) => {
                      event.stopPropagation();
                      onReport(run);
                    }}
                  >
                    <FileText className="h-4 w-4" />
                    {loading === `report:${run.run_id}` ? "loading" : "view report"}
                  </Button>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </TableCell>
              <TableCell>
                {run.output_files?.length ? (
                  <span
                    className="mono block truncate text-xs text-foreground"
                    title={run.output_files.join("\n")}
                  >
                    {run.output_files[0]}
                    {run.output_files.length > 1 ? ` +${run.output_files.length - 1}` : ""}
                  </span>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </TableCell>
              <TableCell>
                <Button
                  variant="outline"
                  size="icon"
                  disabled={active || loading === `trigger:${run.run_id}`}
                  aria-label={`Run ${run.run_id}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onTrigger(run);
                  }}
                >
                  <Play className="h-4 w-4" />
                </Button>
              </TableCell>
            </TableRow>
          ))}
          {!runs.length ? (
            <TableRow>
              <TableCell colSpan={6} className="h-16 text-left text-muted-foreground">
                No runs yet.
              </TableCell>
            </TableRow>
          ) : null}
        </TableBody>
      </Table>
    </div>
  );
}

function Viewer({
  panel,
  theme,
  onSaveScript,
  onSaveInstruction,
  onClose,
}: {
  panel: Panel;
  theme: "light" | "dark";
  onSaveScript: (runId: string, path: string, text: string, saveAs: string) => Promise<{ path: string; run: Run }>;
  onSaveInstruction: (text: string) => Promise<InstructionPayload>;
  onClose: () => void;
}) {
  const title =
    panel.kind === "script"
      ? `${panel.run.run_id} / script`
      : panel.kind === "report"
        ? `${panel.run.run_id} / report`
        : "Report Instruction";

  return (
    <aside className="panel flex min-h-[520px] flex-col overflow-hidden">
      <header className="flex min-h-14 items-center gap-2 border-b px-4">
        <div className="min-w-0 flex-1">
          <div className="mono truncate text-sm font-medium">{title}</div>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close viewer">
          <X className="h-4 w-4" />
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto">
        {panel.kind === "script" ? (
          <SourceViewer
            data={panel.data}
            theme={theme}
            onSave={(path, text, saveAs) => onSaveScript(panel.run.run_id, path, text, saveAs)}
          />
        ) : null}
        {panel.kind === "report" ? <MarkdownViewer path={panel.data.path} text={panel.data.text} /> : null}
        {panel.kind === "instruction" ? <InstructionEditor data={panel.data} theme={theme} onSave={onSaveInstruction} /> : null}
      </div>
    </aside>
  );
}

function InstructionEditor({
  data,
  theme,
  onSave,
}: {
  data: InstructionPayload;
  theme: "light" | "dark";
  onSave: (text: string) => Promise<InstructionPayload>;
}) {
  const [draft, setDraft] = useState(data.text);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(data.text);
    setEditing(false);
    setSaving(false);
  }, [data]);

  const changed = draft !== data.text;

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
    <div className="flex h-full min-h-[520px] flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <div className="mono min-w-0 flex-1 truncate text-sm text-muted-foreground">{data.source}</div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="icon" onClick={() => setEditing(true)} disabled={editing} aria-label="Edit report instruction">
            <Edit3 className="h-4 w-4" />
          </Button>
          {changed ? (
            <Button size="icon" onClick={save} disabled={saving} aria-label="Save report instruction">
              <Save className="h-4 w-4" />
            </Button>
          ) : null}
        </div>
      </div>
      <Editor
        height="100%"
        language="markdown"
        theme={theme === "dark" ? "vs-dark" : "light"}
        value={draft}
        onChange={(value) => setDraft(value || "")}
        options={{
          ...EDITOR_OPTIONS,
          readOnly: !editing,
        }}
      />
    </div>
  );
}

function SourceViewer({
  data,
  theme,
  onSave,
}: {
  data: SourcePayload;
  theme: "light" | "dark";
  onSave: (path: string, text: string, saveAs: string) => Promise<{ path: string; run: Run }>;
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
    setSaveAs("");
    setEditing(false);
    setSaving(false);
  }, [data]);

  useEffect(() => {
    setDraft(file?.text || "");
    setSaveAs(file ? nextScriptPath(file.path, files) : "");
    setEditing(false);
  }, [file?.path, files]);

  if (!file) {
    return <EmptyViewer />;
  }

  const changed = draft !== file.text;

  async function save() {
    if (!changed) return;
    setSaving(true);
    try {
      const result = await onSave(file.path, draft, saveAs || nextScriptPath(file.path, files));
      setFiles((current) =>
        current.some((item) => item.path === result.path)
          ? current.map((item) => item.path === result.path ? { ...item, text: draft } : item)
          : [...current, { path: result.path, text: draft }],
      );
      setSelected(result.path);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex h-full min-h-[520px] flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <select
          className="mono h-9 max-w-full flex-1 rounded-md border bg-card px-3 text-sm md:flex-none"
          value={file.path}
          onChange={(event) => setSelected(event.target.value)}
        >
          {files.map((item) => (
            <option key={item.path} value={item.path}>
              {item.path}
            </option>
          ))}
        </select>
        {editing ? (
          <input
            className="mono h-9 min-w-[180px] flex-1 rounded-md border bg-card px-3 text-sm"
            value={saveAs}
            onChange={(event) => setSaveAs(event.target.value)}
            aria-label="Saved script name"
          />
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="outline"
            size="icon"
            onClick={() => setEditing(true)}
            disabled={editing}
            aria-label="Edit script"
          >
            <Edit3 className="h-4 w-4" />
          </Button>
          {changed ? (
            <Button size="icon" onClick={save} disabled={saving} aria-label="Save script">
              <Save className="h-4 w-4" />
            </Button>
          ) : null}
        </div>
      </div>
      <Editor
        height="100%"
        language={editorLanguage(file.path)}
        theme={theme === "dark" ? "vs-dark" : "light"}
        value={draft}
        onChange={(value) => setDraft(value || "")}
        options={{
          ...EDITOR_OPTIONS,
          readOnly: !editing,
        }}
      />
    </div>
  );
}

function MarkdownViewer({ path, text }: { path: string; text: string }) {
  if (!text) {
    return <EmptyViewer />;
  }

  return (
    <article className="markdown">
      {path ? <p className="mono text-sm text-muted-foreground">{path}</p> : null}
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </article>
  );
}

function EmptyViewer() {
  return (
    <div className="grid min-h-[360px] place-items-center px-6 text-center text-muted-foreground">
      -
    </div>
  );
}
