import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api";
import type {
  Artifact, Document, Experiment, ExperimentPayload, ManifestFile, Repository,
  ReviewSession, Run, RunOverview,
} from "./types";

type ManagedKey = "params";
type View = "bench" | "report";
type RunTab = "source" | "output" | "logs" | "report" | "diff";
type PrimaryView = "overview" | "research";
type Selection =
  | { kind: "registry" }
  | { kind: "overview" }
  | { kind: "file"; path: string }
  | { kind: "managed"; key: ManagedKey }
  | { kind: "script"; script: string }
  | { kind: "research" }
  | { kind: "run"; id: string; tab?: RunTab }
  | { kind: "document"; id: string };

type RunDiffPayload = { available: boolean; base_run_id?: string; changed_files?: string[]; diff: string };

type RunSourcePayload = {
  run_id: string;
  selected: string;
  files: Array<{ path: string; text: string }>;
};
const selectedSourceFile = (files: RunSourcePayload["files"], path: string) => files.find(file => file.path === path) || files[0];
if (import.meta.env.DEV) console.assert(selectedSourceFile([{ path: "a.py", text: "" }, { path: "b.py", text: "" }], "b.py")?.path === "b.py", "Run source selection");

type DraftNote = { scope: string; text: string; selection: Selection; view: View };

const query = new URLSearchParams(location.search);
const initialExperiment = query.get("experiment");
const reviewToken = query.get("review");

const basename = (path: string) => path.split("/").pop() || path;
const short = (value?: string | null) => value ? value.slice(0, 12) : "—";
const fmtDuration = (value?: number | null) => value == null ? "—" : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
const fmtBytes = (value = 0) => value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 ** 2).toFixed(1)} MB`;
const fmtTime = (value?: string | null) => value ? value.replace("T", " · ").replace(/Z$/, "") : "—";
const isRunning = (status?: string) => status === "running" || status === "queued";
const statusClass = (status: string) => status === "success" ? "completed" : status === "canceled" ? "failed" : status;

function variants(data: ExperimentPayload) {
  const groups = new Map<string, Run[]>();
  for (const run of data.runs) {
    const key = run.script_name || "experiment";
    groups.set(key, [...(groups.get(key) || []), run]);
  }
  for (const file of data.files.filter(item => item.role === "entrypoint")) {
    if (![...groups.keys()].some(key => key === file.path || basename(file.path) === key)) groups.set(file.path, []);
  }
  return [...groups.entries()].map(([script, runs]) => ({ script, runs }));
}

function fileForScript(data: ExperimentPayload, script: string) {
  return data.files.find(file => file.path === script || basename(file.path) === script) || null;
}

function escapeHtml(value: string) {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const PYTHON_KEYWORDS = /\b(def|class|import|from|return|if|elif|else|for|while|in|not|and|or|with|as|yield|lambda|None|True|False|async|await|raise|try|except|finally|pass|break|continue)\b/g;
function highlightCodePart(value: string) {
  return escapeHtml(value).replace(PYTHON_KEYWORDS, '<span class="kw">$1</span>').replace(/\b(\d[\d_]*\.?\d*)\b/g, '<span class="num">$1</span>');
}
function highlight(value: string, path: string) {
  if (path.endsWith(".py")) {
    return value.split(/("""[\s\S]*?"""|#[^\n]*|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g)
      .map((part, index) => index % 2 ? `<span class="${part.startsWith("#") ? "c" : "str"}">${escapeHtml(part)}</span>` : highlightCodePart(part)).join("");
  }
  let result = escapeHtml(value);
  if (path.endsWith(".json")) return result.replace(/"([^"\n]*)"(\s*:)/g, '<span class="key">"$1"</span>$2').replace(/\b(true|false|null)\b/g, '<span class="kw">$1</span>').replace(/\b(\d[\d_]*\.?\d*)\b/g, '<span class="num">$1</span>');
  if (path.endsWith(".md")) return result.split("\n").map(line => /^#/.test(line) ? `<span class="kw">${line}</span>` : line.replace(/`([^`]+)`/g, '<span class="str">`$1`</span>')).join("\n");
  return result.replace(/(^|\n)(#[^\n]*)/g, '$1<span class="c">$2</span>');
}

function App() {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [experimentId, setExperimentId] = useState<string | null>(initialExperiment);
  const [data, setData] = useState<ExperimentPayload | null>(null);
  const [selection, setSelection] = useState<Selection>(initialExperiment ? { kind: "overview" } : { kind: "registry" });
  const [primaryView, setPrimaryView] = useState<PrimaryView>("overview");
  const [view, setView] = useState<View>("bench");
  const [filter, setFilter] = useState("");
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    localStorage.getItem("autoexp-theme") === "light" ? "light" : "dark",
  );
  const [railOpen, setRailOpen] = useState(() => localStorage.getItem("autoexp-rail") !== "closed");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");

  useEffect(() => {
    let active = true;
    const load = () => api<{ repositories: Repository[] }>("/api/registry").then(value => {
      if (!active) return;
      setRepositories(value.repositories);
      if (!experimentId) setExperimentId(value.repositories.flatMap(repo => repo.experiments)[0]?.experiment_id || null);
    }).catch(reason => active && setError(String(reason)));
    load();
    const timer = window.setInterval(load, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    if (!experimentId) { setData(null); return; }
    let active = true;
    const load = () => api<ExperimentPayload>(`/api/experiments/${encodeURIComponent(experimentId)}`).then(value => {
      if (active) { setData(value); setError(""); }
    }).catch(reason => active && setError(String(reason)));
    load();
    const timer = window.setInterval(load, 4000);
    const url = new URL(location.href);
    url.searchParams.set("experiment", experimentId);
    history.replaceState(null, "", url);
    return () => { active = false; window.clearInterval(timer); };
  }, [experimentId]);

  useEffect(() => {
    document.documentElement.classList.toggle("light", theme === "light");
    localStorage.setItem("autoexp-theme", theme);
  }, [theme]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 3400);
    return () => window.clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      const target = document.activeElement?.tagName;
      const typing = target === "INPUT" || target === "TEXTAREA" || target === "SELECT";
      if (event.key === "/" && !typing) { event.preventDefault(); document.getElementById("filterBox")?.focus(); return; }
      if (event.key === "Escape") { setFilter(""); setView("bench"); setSelection(current => current.kind === "registry" ? current : { kind: "overview" }); (document.activeElement as HTMLElement | null)?.blur?.(); return; }
      if (typing || !["ArrowDown", "ArrowUp", "j", "k"].includes(event.key)) return;
      const rows = [...document.querySelectorAll<HTMLButtonElement>(".rl")];
      if (!rows.length) return;
      event.preventDefault();
      const current = rows.findIndex(row => row.getAttribute("aria-current") === "true");
      const step = event.key === "ArrowDown" || event.key === "j" ? 1 : -1;
      rows[Math.min(Math.max(current + step, 0), rows.length - 1)]?.click();
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, []);

  const chooseExperiment = (id: string) => { setExperimentId(id); setPrimaryView("overview"); setSelection({ kind: "overview" }); setView("bench"); setFilter(""); };
  const navigate = (next: Selection) => {
    if (next.kind === "research") { setPrimaryView("research"); setSelection({ kind: "overview" }); }
    else { if (next.kind === "overview") setPrimaryView("overview"); setSelection(next); }
    setView("bench");
    document.querySelector(".inspector-scroll")?.scrollTo({ top: 0 });
  };
  const closeInspector = () => { setSelection({ kind: "overview" }); setView("bench"); };
  const current = data?.experiment || repositories.flatMap(repo => repo.experiments).find(item => item.experiment_id === experimentId);
  const activeRun = data?.runs.find(run => isRunning(run.status));
  const statusText = activeRun ? `${activeRun.status} ${short(activeRun.run_id)} · ${basename(activeRun.script_name)}` : data?.research?.loop.active ? `${data.research.loop.phase} · ${data.research.loop.status}` : `idle · ${data?.runs.length || current?.run_count || 0} runs`;
  const inspectorOpen = Boolean(data && selection.kind !== "registry" && (view === "report" || selection.kind !== "overview"));

  return <div className="app">
    <header>
      <button className="wordmark" onClick={() => { setPrimaryView("overview"); setSelection({ kind: "registry" }); setView("bench"); }} title="All experimentations"><b>auto<i>exp</i></b><span className="tag">local autonomous experimentation</span></button>
      <button className="rail-toggle" aria-controls="experiment-rail" aria-expanded={railOpen} onClick={() => setRailOpen(open => { const next = !open; localStorage.setItem("autoexp-rail", next ? "open" : "closed"); return next; })} title={railOpen ? "Collapse sidebar" : "Expand sidebar"} aria-label={railOpen ? "Collapse sidebar" : "Expand sidebar"}><span aria-hidden="true">{railOpen ? "«" : "»"}</span></button>
      <label className="proj-sel">experimentation · <select value={experimentId || ""} onChange={event => chooseExperiment(event.target.value)} aria-label="Experimentation">
        {repositories.map(repo => <optgroup key={repo.repo_id} label={`${repo.title} · ${repo.path}`}>{repo.experiments.map(item => <option key={item.experiment_id} value={item.experiment_id}>{item.title}</option>)}</optgroup>)}
      </select><span>▾</span></label>
      <div className="hd-right">
        {current ? <span className="status"><span className={`dot ${activeRun || data?.research?.loop.active ? "running" : ""}`} />{statusText}</span> : null}
        <div className="view-toggle" role="tablist" aria-label="View"><button aria-pressed={view === "bench"} onClick={() => setView("bench")}>Bench</button><button id="report-toggle" aria-pressed={view === "report"} onClick={() => setView("report")}>Report</button></div>
        {current ? <a className="txtbtn" href={`/api/experiments/${encodeURIComponent(current.experiment_id)}/bundle`}>Bundle</a> : null}
        <button className="theme-toggle" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}><span aria-hidden="true">{theme === "dark" ? "☀" : "☾"}</span></button>
        <button className="runbtn" disabled={!current} onClick={() => current && setToast(`Run from your terminal · autoexp run --experiment ${current.experiment_id}`)}>Run</button>
      </div>
    </header>
    <div className={railOpen ? "body" : "body rail-collapsed"}>
      <Rail data={data} selection={selection} primaryView={primaryView} filter={filter} onFilter={setFilter} onSelect={navigate} />
      <main className="pane">
        <div className={`pane-work${inspectorOpen ? " split" : ""}`}>
          <div className="overview-pane">
            <div className="pane-in">
              {error ? <Empty text={error} /> : selection.kind === "registry" ? <Registry repositories={repositories} onSelect={chooseExperiment} /> : !data ? <Empty text={experimentId ? "Reading the global Autoexp registry…" : "No experimentations yet."} /> : primaryView === "research" && data.research ? <ResearchView data={data} onSelect={navigate} /> : <Overview data={data} onSelect={navigate} />}
            </div>
          </div>
          {inspectorOpen && data ? <aside className="inspector" aria-label={`${scopeLabel(selection, view)} inspector`}>
            <div className="inspector-head">
              <span>{scopeLabel(selection, view)}</span>
              <button onClick={closeInspector} title="Close inspector" aria-label="Close inspector">×</button>
            </div>
            <div className="inspector-scroll">
              <div className="pane-in">
                <div className="detail-view">{view === "report" ? <ProjectReport data={data} onRun={id => navigate({ kind: "run", id, tab: "report" })} /> : <Detail data={data} selection={selection} onSelect={navigate} />}</div>
              </div>
            </div>
          </aside> : null}
        </div>
        <ReviewDock token={reviewToken} selection={selection} view={view} experimentId={experimentId} onSelect={navigate} onView={setView} onToast={setToast} />
      </main>
    </div>
    <StatusBar data={data} selection={selection} view={view} review={Boolean(reviewToken)} />
    {toast ? <div className="toasts"><div className="toast">{toast}</div></div> : null}
  </div>;
}

function Rail({ data, selection, primaryView, filter, onFilter, onSelect }: { data: ExperimentPayload | null; selection: Selection; primaryView: PrimaryView; filter: string; onFilter: (value: string) => void; onSelect: (value: Selection) => void }) {
  const hit = (value: string) => !filter || value.toLowerCase().includes(filter.toLowerCase());
  if (!data) return <nav id="experiment-rail" className="rail" aria-label="Experimentation"><div className="filter"><input id="filterBox" placeholder="filter…" value={filter} onChange={event => onFilter(event.target.value)} /></div></nav>;
  const groups = variants(data);
  const reports = data.documents.filter(item => item.kind === "report");
  const insights = data.documents.filter(item => item.kind === "insight");
  const workspaceGroups = [
    { label: "Scripts", roles: ["entrypoint", "editable-source"] },
    { label: "Inputs", roles: ["supporting-source", "input-data", "frozen-evaluator", "secret-source", "report-guidance"] },
  ];
  const hasParams = Object.keys(data.managed.params || {}).length > 0;
  return <nav id="experiment-rail" className="rail" aria-label="Experimentation">
    <div className="filter"><input id="filterBox" placeholder="filter…" value={filter} onChange={event => onFilter(event.target.value)} /></div>
    <RailRow active={primaryView === "overview"} onClick={() => onSelect({ kind: "overview" })} name="Overview" />
    {workspaceGroups.map(group => { const files = data.files.filter(file => group.roles.includes(file.role) && hit(file.path)); const params = group.label === "Inputs" && hasParams && hit("parameters"); return files.length || params ? <div className="rail-group" key={group.label}>
      <div className="rl-sec">{group.label}</div>
      {files.map(file => { const role = roleShort(file.role); return <RailRow key={file.path} active={selection.kind === "file" && selection.path === file.path} onClick={() => onSelect({ kind: "file", path: file.path })} name={basename(file.path)} mono meta={role[0]} metaClass={role[1]} />; })}
      {params ? <RailRow active={selection.kind === "managed"} onClick={() => onSelect({ kind: "managed", key: "params" })} name="Parameters" meta="input" /> : null}
    </div> : null; })}
    {data.research ? <>
      <div className="rl-sec">Research</div>
      <RailRow active={primaryView === "research"} onClick={() => onSelect({ kind: "research" })} name="The loop" meta={`best ${data.research.objective.best ?? "—"}`} />
      {[...data.research.experiments].sort((a, b) => a.sequence - b.sequence).filter(item => hit(`${item.attempt_id} ${item.hypothesis}`)).map(item => <RailRow key={item.key} active={selection.kind === "run" && selection.id === item.run_id} onClick={() => item.run_id ? onSelect({ kind: "run", id: item.run_id, tab: "diff" }) : onSelect({ kind: "research" })} name={item.attempt_id} mono indent status={item.verdict || item.status} score={item.score == null ? "—" : String(item.score)} title={item.hypothesis} />)}
    </> : <>
      <div className="rl-sec">{groups.length > 1 ? "Variants" : "Script"}</div>
      {groups.filter(group => hit(group.script) || group.runs.some(run => hit(`${run.run_id} ${run.title || ""}`))).map(group => <div className="rail-group" key={group.script}>
        <RailRow active={selection.kind === "script" && selection.script === group.script} onClick={() => onSelect({ kind: "script", script: group.script })} name={basename(group.script)} meta={`${group.runs.length} run${group.runs.length === 1 ? "" : "s"}`} />
        {(filter || selection.kind === "script" && selection.script === group.script || selection.kind === "run" && group.runs.some(run => run.run_id === selection.id)) ? group.runs.filter(run => hit(`${run.run_id} ${run.title || ""}`)).map(run => <RailRow key={run.run_id} active={selection.kind === "run" && selection.id === run.run_id} onClick={() => onSelect({ kind: "run", id: run.run_id })} name={run.title || run.run_id} indent status={run.status} title={`${run.run_id} · ${run.created_at}`} />) : null}
      </div>)}
    </>}
    {reports.length ? <><div className="rl-sec">Reports</div>{reports.filter(item => hit(item.title)).map(item => <RailRow key={item.document_id} active={selection.kind === "document" && selection.id === item.document_id} onClick={() => onSelect({ kind: "document", id: item.document_id })} name={item.title} />)}</> : null}
    {insights.length ? <><div className="rl-sec">Insights</div>{insights.filter(item => hit(item.title)).map(item => <RailRow key={item.document_id} active={selection.kind === "document" && selection.id === item.document_id} onClick={() => onSelect({ kind: "document", id: item.document_id })} name={item.title} />)}</> : null}
  </nav>;
}

function RailRow({ active, onClick, name, mono = false, meta, metaClass = "", indent = false, status, score, title }: { active: boolean; onClick: () => void; name: string; mono?: boolean; meta?: string; metaClass?: string; indent?: boolean; status?: string; score?: string; title?: string }) {
  return <button className={`rl${indent ? " indent" : ""}`} aria-current={active} onClick={onClick} title={title}>{status ? <span className={`sdot ${statusClass(status)}`}>●</span> : null}<span className={`nm${mono ? " mono" : ""}`}>{name}</span>{score ? <span className="score">{score}</span> : meta ? <span className={`rr ${metaClass}`}>{meta}</span> : null}</button>;
}

function Registry({ repositories, onSelect }: { repositories: Repository[]; onSelect: (id: string) => void }) {
  const experiments = repositories.flatMap(repo => repo.experiments.map(item => ({ ...item, repository: repo })));
  return <><h1 className="title">Experimentations</h1><p className="lede">One package, installed once. Every experimentation your agents run—across every repository—indexed centrally and served from the same local UI.</p><div className="kv"><span>registry <b>global Autoexp data</b></span><span>server <b>localhost · autoexp view</b></span></div>
    <section>{experiments.length ? <table className="t"><thead><tr><th>experimentation</th><th>repo</th><th>kind</th><th className="num">runs</th><th>status</th><th className="num">updated</th></tr></thead><tbody>{experiments.map(item => <tr className="rowlink" key={item.experiment_id} onClick={() => onSelect(item.experiment_id)}><td className="serif">{item.title}</td><td className="mono">{item.repository.title}</td><td className="mono">{item.kind}</td><td className="num">{item.run_count || 0}</td><td><Status value={item.latest_run_status || item.status} /></td><td className="num">{fmtTime(item.latest_run_at || item.updated_at)}</td></tr>)}</tbody></table> : <Empty text="No experimentations yet. Create one from an existing Git worktree." />}</section>
    <section><div className="lbl">Start a new one</div><p className="empty-copy">From your coding agent, describe the objective and use the Autoexp skill; or run <span className="mono">autoexp experiment create "&lt;objective&gt;"</span>. No repository scaffolding or separate project directory.</p></section></>;
}

function Detail({ data, selection, onSelect }: { data: ExperimentPayload; selection: Selection; onSelect: (value: Selection) => void }) {
  if (selection.kind === "file") return <FileView data={data} path={selection.path} />;
  if (selection.kind === "managed") return <ManagedView data={data} managedKey={selection.key} />;
  if (selection.kind === "script") return <ScriptView data={data} script={selection.script} onRun={(id, tab) => onSelect({ kind: "run", id, tab })} />;
  if (selection.kind === "research" && data.research) return <ResearchView data={data} onSelect={onSelect} />;
  if (selection.kind === "run") { const run = data.runs.find(item => item.run_id === selection.id); return run ? <RunView data={data} run={run} initialTab={selection.tab} onRun={id => onSelect({ kind: "run", id })} /> : <Empty text="Run not found." />; }
  if (selection.kind === "document") { const document = data.documents.find(item => item.document_id === selection.id); return document ? <DocumentView data={data} document={document} onRun={id => onSelect({ kind: "run", id })} /> : <Empty text="Document not found." />; }
  return <Overview data={data} onSelect={onSelect} />;
}

function Overview({ data, onSelect }: { data: ExperimentPayload; onSelect: (value: Selection) => void }) {
  const done = data.runs.filter(run => run.status === "success").length;
  const failed = data.runs.filter(run => ["failed", "canceled"].includes(run.status)).length;
  const groups = variants(data);
  const reportCount = data.documents.filter(item => item.kind === "report").length + data.runs.filter(run => run.report_path).length;
  const reportDocument = data.documents.find(item => item.path === data.project_report.path);
  const reportHeading = data.project_report.text.match(/^#\s+(.+)$/m)?.[1] || "";
  const reportExcerpt = data.project_report.text.replace(/^#\s+.*$/m, "").replace(/[#*_`>\[\]()]/g, " ").replace(/\s+/g, " ").trim().slice(0, 240);
  return <div className="primary-view"><div className="primary-fixed"><h1 className="title">{data.experiment.title}</h1><p className="lede">{data.experiment.objective}</p><div className="kv"><span>repo <b>{data.experiment.repo_path}</b></span><span>kind <b>{data.experiment.kind}</b></span><span>runner <b>{data.experiment.runner}</b></span><span>experiment <b>{data.experiment.experiment_id}</b></span></div>
    <div className="counters"><Counter label="runs" value={data.runs.length} /><Counter label="completed" value={done} tone="ok" note={data.runs.length ? `${Math.round(done / data.runs.length * 100)}%` : ""} /><Counter label="failed" value={failed} tone={failed ? "bad" : ""} />{data.research ? <Counter label="best score" value={data.research.objective.best ?? "—"} tone="ok" note={data.research.objective.baseline == null ? "" : `baseline ${data.research.objective.baseline}`} /> : <Counter label="reports" value={reportCount} />}</div>
    </div><div className="primary-scroll">
    {data.research ? <section><div className="lbl">The loop</div><table className="t"><tbody><tr className="rowlink" onClick={() => onSelect({ kind: "research" })}><td className="serif">Evolve the candidate against one frozen evaluator—{data.research.experiments.length} attempts, {data.research.experiments.filter(item => item.verdict === "kept").length} kept</td><td className="num">best {data.research.objective.best ?? "—"}</td><td className="mono right-cell">open →</td></tr></tbody></table></section> : <section><div className="lbl">{groups.length > 1 ? "Variants" : "Runs"} <span className="path">— immutable source and evidence per execution</span></div>{groups.length > 1 ? <VariantTable groups={groups} onSelect={script => onSelect({ kind: "script", script })} /> : <RunTable runs={data.runs} onRun={(id, tab) => onSelect({ kind: "run", id, tab })} />}</section>}
    {data.milestones.length ? <section><div className="lbl">Milestones <span className="path">— decisions, surprises, and new bests</span></div>{data.milestones.map(item => <Milestone key={item.milestone_id} item={item} onRun={id => onSelect({ kind: "run", id })} />)}</section> : null}
    <section><div className="lbl">Report</div>{data.project_report.exists ? <><p className="lede report-excerpt">{reportHeading ? <><strong>{reportHeading}</strong>{" "}</> : null}{reportExcerpt}…</p><p><button className="link" onClick={() => window.dispatchEvent(new CustomEvent("autoexp-report"))}>Open the report view →</button>{reportDocument ? <> <span className="sep">·</span> <a className="link" href={`/api/experiments/${data.experiment.experiment_id}/documents?path=${encodeURIComponent(reportDocument.path)}&download=1`}>Download report.md</a></> : null}</p></> : <p className="empty-copy">No project report yet. Reports and insights appear here once agents add immutable documents to the global experiment record.</p>}</section>
    </div></div>;
}

function Counter({ label, value, tone = "", note = "" }: { label: string; value: string | number; tone?: string; note?: string }) { return <div className="counter"><div className="k">{label}</div><div className={`v ${tone}`}>{value}{note ? <small>{note}</small> : null}</div></div>; }
function Status({ value }: { value: string }) { return <span className={`st ${statusClass(value)}`}>{value}</span>; }

function VariantTable({ groups, onSelect }: { groups: Array<{ script: string; runs: Run[] }>; onSelect: (script: string) => void }) {
  const max = Math.max(...groups.map(group => group.runs.length), 1);
  return <table className="t"><thead><tr><th>variant</th><th>result so far</th><th className="bar-col">evidence</th><th className="num">runs</th></tr></thead><tbody>{groups.map(group => { const latest = group.runs[0]; return <tr className="rowlink" key={group.script} onClick={() => onSelect(group.script)}><td className="serif">{basename(group.script)}</td><td>{latest ? <Status value={latest.status} /> : <span className="faint">not run</span>}</td><td><div className="bar" title={`${group.runs.length} runs`}><i style={{ width: `${group.runs.length / max * 100}%` }} /></div></td><td className="num">{group.runs.length}</td></tr>; })}</tbody></table>;
}

function RunTable({ runs, onRun }: { runs: Run[]; onRun: (id: string, tab?: RunTab) => void }) {
  return runs.length ? <table className="t run-table"><thead><tr><th>run</th><th>title</th><th>status</th><th>inspect</th><th className="num">when</th></tr></thead><tbody>{runs.map(run => <tr className="rowlink" key={run.run_id} onClick={() => onRun(run.run_id, "source")}><td className="mono">{short(run.run_id)}{run.parent_run_id ? <span className="faint"> ↳ {short(run.parent_run_id)}</span> : null}</td><td className="serif">{run.title || basename(run.script_name)}</td><td><Status value={run.status} /></td><td><div className="evidence-links"><button onClick={event => { event.stopPropagation(); onRun(run.run_id, "source"); }}>source</button><button onClick={event => { event.stopPropagation(); onRun(run.run_id, "output"); }}>artifacts</button><button onClick={event => { event.stopPropagation(); onRun(run.run_id, "logs"); }}>logs</button>{run.report_path ? <button onClick={event => { event.stopPropagation(); onRun(run.run_id, "report"); }}>report</button> : null}<button onClick={event => { event.stopPropagation(); onRun(run.run_id, "diff"); }}>diff</button></div></td><td className="num">{fmtTime(run.created_at)}</td></tr>)}</tbody></table> : <p className="empty-copy">No runs yet. The first execution will seal the declared inputs.</p>;
}

function Milestone({ item, onRun }: { item: ExperimentPayload["milestones"][number]; onRun: (id: string) => void }) {
  const value = `${item.title} ${item.significance}`.toLowerCase();
  const tag = value.includes("best") ? "best" : value.includes("surpris") ? "surprise" : "decision";
  return <div className="mile"><span className={`tag ${tag}`}>{item.title}</span><span className="txt">{item.significance}</span>{item.target_kind === "run" ? <button className="ref link" onClick={() => onRun(item.target_id)}>{short(item.target_id)}</button> : <span className="ref">{item.target_id}</span>}</div>;
}

function managedPath(_key: ManagedKey) { return "parameters.json"; }
function ManagedView({ data, managedKey }: { data: ExperimentPayload; managedKey: ManagedKey }) {
  const value = data.managed[managedKey];
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return <><h1 className="title">Run parameters</h1><div className="kv"><span><b>input</b></span><span>values used by every execution</span></div><section className="viewer-section"><CodeViewer path={managedPath(managedKey)} text={text} right="current values" sealed="read-only" /></section></>;
}

function FileView({ data, path }: { data: ExperimentPayload; path: string }) {
  const file = data.files.find(item => item.path === path);
  const [version, setVersion] = useState("live");
  const [payload, setPayload] = useState<{ text: string | null; live?: boolean } | null>(null);
  useEffect(() => { setVersion("live"); }, [path]);
  useEffect(() => {
    if (!file || file.role === "secret-source") return;
    setPayload(null);
    const snapshot = version === "live" ? "" : `&snapshot=${encodeURIComponent(version)}`;
    api<{ text: string | null; live?: boolean }>(`/api/experiments/${data.experiment.experiment_id}/files?path=${encodeURIComponent(path)}${snapshot}`).then(setPayload).catch(() => setPayload({ text: null, live: false }));
  }, [data.experiment.experiment_id, file?.role, path, version]);
  if (!file) return <Empty text="Declared file not found." />;
  if (file.role === "secret-source") return <SecretView file={file} />;
  const versions = data.runs.filter(run => run.source_snapshot_id).filter((run, index, items) => items.findIndex(item => item.source_snapshot_id === run.source_snapshot_id) === index);
  const csv = payload && path.toLowerCase().endsWith(".csv") ? parseCsv(payload.text || "") : null;
  return <><h1 className="title mono">{file.path}</h1><div className="kv"><span><b>[{roleShort(file.role)[0]}]</b></span><span>{file.description || "Declared repository file"}</span><span>availability <b>{file.available ? "live" : "missing"}</b></span><span>hash <b>{short(file.content_hash)}</b></span></div><section className="viewer-section">{versions.length ? <div className="codehead"><span>as of</span><select className="small-select" value={version} onChange={event => setVersion(event.target.value)}><option value="live">current working copy</option>{versions.map(run => <option key={run.source_snapshot_id!} value={run.source_snapshot_id!}>{short(run.run_id)} · {fmtTime(run.created_at)}</option>)}</select></div> : null}{csv ? <TablePreview columns={csv[0] || []} rows={csv.slice(1)} /> : <CodeViewer path={file.path} text={payload?.text || ""} loading={!payload} right={version === "live" ? "current · sealed on the next run" : `historical · ${short(version)}`} sealed={version === "live" ? "read-only" : "sealed"} />}</section></>;
}

function SecretView({ file }: { file: ManifestFile }) {
  return <><h1 className="title mono">{file.path}</h1><div className="kv"><span><b>[secrets]</b></span><span>machine-local values · handed to the runner · never snapshotted, indexed, or served</span></div><div className="detail-scroll"><section><div className="lbl">Keys <span className="path">— values are intentionally unavailable in this UI</span></div><div className="secret-box">{file.secret_keys.length ? file.secret_keys.map(key => <div className="frow static" key={key.name}><span>{key.name}</span><span className="masked">= ••••••••••••</span><span className={`fnote ${key.populated ? "available" : ""}`}>{key.populated ? "available" : "empty"}</span></div>) : <p className="empty-copy">No keys recorded.</p>}</div></section><section><p className="empty-copy">The record stores only key names and availability. Secret values never cross the runner handoff boundary.</p></section></div></>;
}

function CodeViewer({ path, text, right, sealed, loading = false }: { path: string; text: string; right?: string; sealed?: string; loading?: boolean }) {
  const value = loading ? "" : text || "File unavailable.";
  const gutter = value.split("\n").map((_, index) => index + 1).join("\n");
  return <div className="ed"><div className="ed-h"><span>{path}</span><span className="grow" />{right ? <span className="ed-note">{right}</span> : null}{sealed ? <span className="act off">{sealed}</span> : null}</div><div className="ed-b"><div className="gut">{gutter}</div><pre className="src" dangerouslySetInnerHTML={{ __html: loading ? "" : highlight(value, path) }} /></div></div>;
}

function ScriptView({ data, script, onRun }: { data: ExperimentPayload; script: string; onRun: (id: string, tab?: RunTab) => void }) {
  const [tab, setTab] = useState<"source" | "runs" | "result" | "report">("source");
  useEffect(() => setTab("source"), [script]);
  const file = fileForScript(data, script);
  const runs = data.runs.filter(run => run.script_name === script || basename(file?.path || "") === run.script_name);
  const latest = runs.find(run => run.status === "success") || runs[0];
  return <><div className="sticky-view-head"><h1 className="title">{basename(script)}</h1><p className="lede">{file?.description || "One independently recorded experiment variant."}</p><div className="kv"><span>script <b>{file?.path || script}</b></span><span>runs <b>{runs.length}</b></span>{latest ? <span>latest <b>{latest.status}</b></span> : null}</div><Tabs values={["source", "runs", "result", "report"]} active={tab} onChange={value => setTab(value as typeof tab)} /></div><div className="tabbody evidence-body">{tab === "source" ? file ? <LiveFile experimentId={data.experiment.experiment_id} file={file} /> : <Empty text="No declared source file matches this variant." /> : tab === "runs" ? <RunTable runs={runs} onRun={onRun} /> : tab === "result" ? latest ? <OutputPane run={latest} /> : <Empty text="No result yet." /> : latest ? <RunReportPane run={latest} runs={data.runs} onRun={onRun} /> : <Empty text="No report yet." />}</div></>;
}

function LiveFile({ experimentId, file }: { experimentId: string; file: ManifestFile }) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => { api<{ text: string | null }>(`/api/experiments/${experimentId}/files?path=${encodeURIComponent(file.path)}`).then(value => setText(value.text)); }, [experimentId, file.path]);
  return <CodeViewer path={file.path} text={text || ""} loading={text == null} right="current · every run seals its own copy" sealed="read-only" />;
}

function ResearchView({ data, onSelect }: { data: ExperimentPayload; onSelect: (value: Selection) => void }) {
  const research = data.research!;
  const attempts = [...research.experiments].sort((a, b) => a.sequence - b.sequence);
  const finalFiles = data.files.filter(item => item.role === "editable-source");
  const roleFile = (role: string) => research.files.find(item => item.role === role);
  return <div className="primary-view"><div className="primary-fixed"><h1 className="title">The loop</h1><p className="lede">One scalar, one frozen judge, one editable candidate. Every hypothesis stays in the ledger, kept or not.</p><div className="kv"><span>metric <b>{research.objective.metric}</b></span><span>direction <b>{research.objective.direction}</b></span><span>baseline <b>{research.objective.baseline ?? "—"}</b></span><span>best <b>{research.objective.best ?? "—"}</b></span><span>loop <b>{research.loop.active ? research.loop.phase : research.loop.status}</b></span>{[["human", "objective"], ["agent", "candidate"], ["frozen", "evaluator"]].map(([role, label]) => { const item = roleFile(role); return item ? <span key={role}>{label} <button className="link mono" onClick={() => onSelect({ kind: "file", path: item.path })}>{basename(item.path)}</button></span> : null; })}</div><section className="chart-section"><ScoreChart data={data} /></section></div><div className="primary-scroll">{finalFiles.length ? <section><div className="lbl">Final state <span className="path">— current editable files produced by the loop</span></div><table className="t"><tbody>{finalFiles.map(file => <tr className="rowlink" key={file.path} onClick={() => onSelect({ kind: "file", path: file.path })}><td className="serif">{basename(file.path)}</td><td className="mono faint">{file.path}</td><td className="mono right-cell">open →</td></tr>)}</tbody></table></section> : null}<section><div className="lbl">Attempts</div><table className="t"><thead><tr><th>attempt</th><th>hypothesis</th><th className="num">score</th><th>verdict</th></tr></thead><tbody>{attempts.map(item => <tr className={item.run_id ? "rowlink" : ""} key={item.key} onClick={() => item.run_id && onSelect({ kind: "run", id: item.run_id, tab: "diff" })}><td className="mono">{item.attempt_id}</td><td className="serif">{item.hypothesis}</td><td className="num">{item.score ?? "—"}</td><td className={`mono verdict ${item.verdict || item.status}`}>{item.verdict || item.status}</td></tr>)}</tbody></table></section></div></div>;
}

function chartTooltipX(point: number, left: number, right: number, width: number, tooltipWidth: number) { return Math.min(Math.max(point - tooltipWidth / 2, left), width - right - tooltipWidth); }
if (import.meta.env.DEV) console.assert(chartTooltipX(710, 58, 18, 720, 300) === 402, "Chart tooltip clamp");

function ScoreChart({ data }: { data: ExperimentPayload }) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const research = data.research!;
  const attempts = [...research.experiments].sort((a, b) => a.sequence - b.sequence).filter(item => item.score != null);
  if (!attempts.length) return <p className="empty-copy">No scored attempts yet.</p>;
  const width = 720, height = 240, left = 58, right = 18, top = 18, bottom = 42;
  const values = attempts.map(item => Number(item.score));
  if (research.objective.baseline != null) values.push(research.objective.baseline);
  let low = Math.min(...values), high = Math.max(...values);
  if (low === high) { low -= 1; high += 1; }
  const rangePad = (high - low) * .12;
  low -= rangePad;
  high += rangePad;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const x = (index: number) => left + index * (plotWidth / Math.max(attempts.length - 1, 1));
  const y = (value: number) => top + ((high - value) / (high - low)) * plotHeight;
  const label = (value: number) => Number(value.toFixed(2)).toString();
  const yTicks = Array.from({ length: 5 }, (_, index) => low + (high - low) * index / 4);
  const xStep = Math.max(1, Math.ceil(attempts.length / 7));
  const xTicks = attempts.filter((_, index) => index % xStep === 0 || index === attempts.length - 1);
  const path = attempts.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)} ${y(Number(item.score)).toFixed(1)}`).join(" ");
  const retained = attempts.filter(item => item.verdict !== "reverted");
  const candidates = retained.length ? retained : attempts;
  const best = candidates.reduce((chosen, item) => research.objective.direction === "max" ? Number(item.score) > Number(chosen.score) ? item : chosen : Number(item.score) < Number(chosen.score) ? item : chosen, candidates[0]);
  const hovered = hoveredIndex == null ? null : attempts[hoveredIndex];
  const tooltipWidth = 300;
  const tooltipLeft = hoveredIndex == null ? 0 : chartTooltipX(x(hoveredIndex), left, right, width, tooltipWidth);
  const tooltipTop = hovered ? Math.min(Math.max(y(Number(hovered.score)) - 58, top + 6), height - bottom - 54) : 0;
  const tooltipDetail = hovered ? hovered.hypothesis.length > 54 ? `${hovered.hypothesis.slice(0, 53)}…` : hovered.hypothesis : "";
  return <div className="chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${research.objective.metric} by attempt`}><rect className="chart-plot" x={left} y={top} width={plotWidth} height={plotHeight} />{yTicks.map(value => <g className="chart-tick" key={value}><line x1={left} y1={y(value)} x2={width - right} y2={y(value)} /><text x={left - 9} y={y(value) + 4} textAnchor="end">{label(value)}</text></g>)}{xTicks.map(item => { const index = attempts.indexOf(item); return <g className="chart-x-tick" key={item.key}><line x1={x(index)} y1={height - bottom} x2={x(index)} y2={height - bottom + 5} /><text x={x(index)} y={height - bottom + 18} textAnchor="middle">{item.attempt_id}</text></g>; })}{research.objective.baseline != null ? <g className="chart-baseline"><line x1={left} y1={y(research.objective.baseline)} x2={width - right} y2={y(research.objective.baseline)} /><text x={width - right - 4} y={y(research.objective.baseline) - 5} textAnchor="end">baseline {label(research.objective.baseline)}</text></g> : null}<path className="chart-line" d={path} />{attempts.map((item, index) => <circle className={`chart-point ${item.verdict || item.status}`} key={item.key} cx={x(index)} cy={y(Number(item.score))} r="4" tabIndex={0} aria-label={`${item.attempt_id}, ${research.objective.metric} ${label(Number(item.score))}, ${item.verdict || item.status}. ${item.hypothesis}`} onMouseEnter={() => setHoveredIndex(index)} onMouseLeave={() => setHoveredIndex(null)} onFocus={() => setHoveredIndex(index)} onBlur={() => setHoveredIndex(null)} />)}<circle className="chart-best" cx={x(attempts.indexOf(best))} cy={y(Number(best.score))} r="8" /><text className="chart-axis-title" x={left + plotWidth / 2} y={height - 4} textAnchor="middle">attempt</text><text className="chart-axis-title" x={13} y={top + plotHeight / 2} textAnchor="middle" transform={`rotate(-90 13 ${top + plotHeight / 2})`}>{research.objective.metric}</text>{hovered ? <g className="chart-tooltip" pointerEvents="none"><rect x={tooltipLeft} y={tooltipTop} width={tooltipWidth} height="46" rx="4" /><text className="main" x={tooltipLeft + 10} y={tooltipTop + 17}>{hovered.attempt_id} · {research.objective.metric} {label(Number(hovered.score))} · {hovered.verdict || hovered.status}</text><text className="sub" x={tooltipLeft + 10} y={tooltipTop + 35}>{tooltipDetail}</text></g> : null}</svg><div className="cap"><span className="brand">— {research.objective.metric}</span>{research.objective.baseline != null ? <span>┄ baseline {research.objective.baseline}</span> : null}<span className="pass">● kept</span><span className="fail">● reverted</span><span>hover points for details</span></div></div>;
}

function RunView({ data, run, initialTab, onRun }: { data: ExperimentPayload; run: Run; initialTab?: RunTab; onRun: (id: string) => void }) {
  const [tab, setTab] = useState<RunTab>(initialTab || "source");
  useEffect(() => setTab(initialTab || "source"), [initialTab, run.run_id]);
  const tabs = ["source", "output", "logs", "report", "diff"];
  return <><div className="sticky-view-head"><h1 className="title run-title">{run.title || basename(run.script_name)}</h1><div className="kv"><span>run <b>{run.run_id}</b></span><span>status <b>{run.status}</b></span><span>exit <b>{run.exit_code ?? "—"}</b></span><span>duration <b>{fmtDuration(run.duration_ms)}</b></span><span>snapshot <b>{short(run.source_snapshot_id)}</b></span>{run.parent_run_id ? <span>child of <button className="link mono" onClick={() => onRun(run.parent_run_id!)}>{short(run.parent_run_id)}</button></span> : null}<span>runner <b>{run.runner_identity || run.runner || data.experiment.runner}</b></span></div><Tabs values={tabs} active={tab} onChange={value => setTab(value as typeof tab)} /></div><div className="tabbody evidence-body">{tab === "source" ? <RunSourcePane run={run} /> : tab === "output" ? <OutputPane run={run} /> : tab === "logs" ? <LogsPane run={run} /> : tab === "report" ? <RunReportPane run={run} runs={data.runs} onRun={onRun} /> : <DeltaPane run={run} />}</div></>;
}

function Tabs({ values, active, onChange }: { values: string[]; active: string; onChange: (value: string) => void }) { return <div className="tabs">{values.map(value => <button className="tab" aria-selected={active === value} key={value} onClick={() => onChange(value)}>{value[0].toUpperCase() + value.slice(1)}</button>)}</div>; }

function RunSourcePane({ run }: { run: Run }) {
  const [source, setSource] = useState<RunSourcePayload | null>(null);
  const [selected, setSelected] = useState("");
  useEffect(() => {
    setSource(null);
    api<RunSourcePayload>(`/api/runs/${run.run_id}/source`).then(value => { setSource(value); setSelected(value.selected); });
  }, [run.run_id]);
  if (!source) return <Loading />;
  const file = selectedSourceFile(source.files, selected);
  if (!file) return <Empty text="No source was sealed with this run." />;
  return <div className="artifact-browser source-browser"><div className="artifact-files"><div className="artifact-label">Snapshot source · {source.files.length}</div>{source.files.map(item => <button className={`frow${item.path === file.path ? " selected" : ""}`} key={item.path} onClick={() => setSelected(item.path)}><span>{basename(item.path)}</span><span className="fnote">{item.path}</span></button>)}</div><div className="artifact-detail"><div className="codehead"><span>{file.path}</span><span className="right">sealed at {short(run.source_snapshot_id)} · immutable evidence</span></div><CodeViewer path={`runs/${run.run_id}/source/${file.path}`} text={file.text} /></div></div>;
}

function useRunOverview(run: Run) {
  const [overview, setOverview] = useState<RunOverview | null>(null);
  useEffect(() => {
    let active = true;
    const load = () => api<RunOverview>(`/api/runs/${run.run_id}`).then(value => active && setOverview(value));
    load();
    const timer = isRunning(run.status) ? window.setInterval(load, 2000) : 0;
    return () => { active = false; if (timer) window.clearInterval(timer); };
  }, [run.run_id, run.status]);
  return overview;
}

function OutputPane({ run }: { run: Run }) {
  const overview = useRunOverview(run);
  if (!overview) return <Loading />;
  return <ArtifactPane run={run} artifacts={(overview.artifact_summary?.artifacts || []).filter(item => item.category === "output")} />;
}

function ArtifactPane({ run, artifacts }: { run: Run; artifacts: Artifact[] }) {
  const [selected, setSelected] = useState<Artifact | null>(artifacts[0] || null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  useEffect(() => { setSelected(artifacts[0] || null); setDetail(null); }, [run.run_id, artifacts.map(item => item.artifact_id).join(":")]);
  useEffect(() => { if (selected && artifacts.some(item => item.artifact_id === selected.artifact_id)) { setDetail(null); api<Record<string, unknown>>(`/api/runs/${run.run_id}/artifacts/${selected.artifact_id}`).then(setDetail); } }, [run.run_id, selected?.artifact_id]);
  if (!artifacts.length) return <p className="empty-copy">No output files. If the run failed before writing any, the logs carry the evidence.</p>;
  return <div className="artifact-browser"><div className="artifact-files"><div className="artifact-label">Output artifacts · {artifacts.length}</div>{artifacts.map(item => <button className={`frow ${selected?.artifact_id === item.artifact_id ? "selected" : ""}`} key={item.artifact_id} onClick={() => setSelected(item)}><span>{basename(item.path)}</span><span className="fnote">{fmtBytes(item.size_bytes)} · {item.media_type}</span></button>)}</div><div className="artifact-detail">{selected ? <><div className="codehead"><span>{selected.path}</span><span className="right">{selected.media_type} · {fmtBytes(selected.size_bytes)} · <a className="link" href={`/api/runs/${run.run_id}/artifacts/${selected.artifact_id}/content?download=1`}>download</a></span></div><ArtifactPreview detail={detail} path={selected.path} /></> : null}</div></div>;
}

function parseCsv(text: string) {
  const rows: string[][] = [[]];
  let cell = "", quoted = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (char === "\"" && quoted && text[index + 1] === "\"") { cell += "\""; index++; }
    else if (char === "\"") quoted = !quoted;
    else if (char === "," && !quoted) { rows[rows.length - 1].push(cell); cell = ""; }
    else if ((char === "\n" || char === "\r") && !quoted) { if (char === "\r" && text[index + 1] === "\n") index++; rows[rows.length - 1].push(cell); cell = ""; rows.push([]); }
    else cell += char;
  }
  rows[rows.length - 1].push(cell);
  if (rows[rows.length - 1].length === 1 && !rows[rows.length - 1][0]) rows.pop();
  return rows;
}

if (import.meta.env.DEV) console.assert(parseCsv("a,b\n\"x,y\",z")[1][0] === "x,y", "CSV preview parser");

function TablePreview({ columns, rows, truncated = false }: { columns: string[]; rows: unknown[][]; truncated?: boolean }) {
  return <div className="table-preview"><table><thead><tr>{columns.map((column, cell) => <th key={`${column}-${cell}`}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column, cell) => <td key={`${column}-${cell}`}>{artifactText(row[cell])}</td>)}</tr>)}</tbody></table>{truncated ? <p className="preview-note">Bounded preview · additional rows omitted.</p> : null}</div>;
}

function artifactText(value: unknown) {
  return value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
}

function ArtifactPreview({ detail, path }: { detail: Record<string, unknown> | null; path: string }) {
  if (!detail) return <Loading />;
  const preview = detail.preview as Record<string, unknown>;
  if (preview.kind === "image") return <div className="image-preview"><img src={String(preview.content_url)} alt={path} /><span>{artifactText(detail.media_type)} · {fmtBytes(Number(detail.size_bytes || 0))}</span></div>;
  if (preview.kind === "csv") {
    const columns = Array.isArray(preview.columns) ? preview.columns.map(String) : [];
    const rows = Array.isArray(preview.rows) ? preview.rows as unknown[][] : [];
    return <TablePreview columns={columns} rows={rows} truncated={Boolean(preview.truncated)} />;
  }
  if (preview.kind === "json") return <CodeViewer path={path} text={JSON.stringify(preview.value, null, 2)} sealed="JSON preview" />;
  if (preview.kind === "text") return <CodeViewer path={path} text={String(preview.text || "")} sealed={preview.truncated ? "truncated text preview" : "text preview"} />;
  return <div className="binary-preview"><b>Binary preview unavailable</b><span>{artifactText(detail.media_type)} · {fmtBytes(Number(detail.size_bytes || 0))}</span><small>Download the immutable artifact to inspect it.</small></div>;
}

function LogsPane({ run }: { run: Run }) {
  const [logs, setLogs] = useState<{ stdout: string; stderr: string } | null>(null);
  useEffect(() => {
    let active = true;
    const load = () => Promise.all([api<{ text: string }>(`/api/runs/${run.run_id}/logs/stdout`), api<{ text: string }>(`/api/runs/${run.run_id}/logs/stderr`)]).then(([stdout, stderr]) => active && setLogs({ stdout: stdout.text, stderr: stderr.text }));
    load();
    const timer = isRunning(run.status) ? window.setInterval(load, 1500) : 0;
    return () => { active = false; if (timer) window.clearInterval(timer); };
  }, [run.run_id, run.status]);
  if (!logs) return <Loading />;
  const lines = [...(logs.stdout.trimEnd() ? logs.stdout.trimEnd().split("\n").map(text => ({ text, error: false })) : []), ...(logs.stderr.trimEnd() ? logs.stderr.trimEnd().split("\n").map(text => ({ text, error: true })) : [])];
  return <div className="log"><div className="logbar"><span className={isRunning(run.status) ? "live" : "idle"}>{isRunning(run.status) ? <><span className="d" />live process</> : `${run.status} · exit ${run.exit_code ?? "—"}`}</span><span className="log-links"><a className="link" href={`/api/runs/${run.run_id}/logs/stdout?download=1`}>stdout</a><a className="link" href={`/api/runs/${run.run_id}/logs/stderr?download=1`}>stderr</a></span></div><div className="logview">{lines.length ? lines.map((line, index) => <div className={line.error ? "err" : "ln"} key={index}>{line.text || " "}</div>) : <div className="dim">no process output</div>}{isRunning(run.status) ? <span className="cursor" /> : null}</div></div>;
}

function RunReportPane({ run, runs, onRun }: { run: Run; runs: Run[]; onRun: (id: string) => void }) {
  const [report, setReport] = useState<{ path: string; text: string; artifact?: Artifact | null } | null>(null);
  useEffect(() => { setReport(null); api<{ path: string; text: string; artifact?: Artifact | null }>(`/api/runs/${run.run_id}/report`).then(setReport); }, [run.run_id]);
  if (!report) return <Loading />;
  return report.text ? <div className="doc"><div className="sub">{report.path} · <a className="link" href={`/api/runs/${run.run_id}/report?download=1`}>download</a></div><MarkdownDoc text={report.text} runs={runs} onRun={onRun} /></div> : <p className="empty-copy">No report was recorded for this run. Outputs, logs, params, and source remain available as the report bundle.</p>;
}

function UnifiedDiff({ text }: { text: string }) {
  const visible = text.split(/(?=^diff --git )/m).filter(block => !/^diff --git a\/\.autoexp\//.test(block)).join("");
  if (!visible.trim()) return <p className="diff-empty">No user source changes.</p>;
  let oldLine: number | null = null, newLine: number | null = null;
  const lines = visible.split("\n");
  return <div className="diff">{lines.map((line, index) => {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    let oldNumber: number | null = null, newNumber: number | null = null;
    if (hunk) { oldLine = Number(hunk[1]); newLine = Number(hunk[2]); }
    else if (oldLine !== null && newLine !== null && !line.startsWith("\\")) {
      if (line.startsWith("+") && !line.startsWith("+++")) newNumber = newLine++;
      else if (line.startsWith("-") && !line.startsWith("---")) oldNumber = oldLine++;
      else if (!line.startsWith("---") && !line.startsWith("+++")) { oldNumber = oldLine++; newNumber = newLine++; }
    }
    if (hunk || /^(diff --git |index |--- |\+\+\+ )/.test(line) || index === lines.length - 1 && !line) return null;
    const kind = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
    return <div className={`dl ${kind}`} key={index}><span className="diff-ln">{oldNumber ?? ""}</span><span className="diff-ln">{newNumber ?? ""}</span><span className="diff-code">{line || " "}</span></div>;
  })}</div>;
}

function DeltaPane({ run }: { run: Run }) {
  const [delta, setDelta] = useState<RunDiffPayload | null>(null);
  useEffect(() => { setDelta(null); api<RunDiffPayload>(`/api/runs/${run.run_id}/diff`).then(setDelta); }, [run.run_id]);
  if (!delta) return <Loading />;
  return delta.available ? <div className="diff-shell"><div className="codehead"><span>source diff</span><span className="right">{delta.base_run_id || run.parent_run_id ? `change vs ${short(delta.base_run_id || run.parent_run_id)}` : "change from prior snapshot"}</span></div>{delta.changed_files?.length ? <div className="diff-summary">{delta.changed_files.map(file => <span key={file}>{file}</span>)}</div> : null}<UnifiedDiff text={delta.diff} /></div> : <p className="empty-copy">No earlier source snapshot is available for comparison.</p>;
}

function ProjectReport({ data, onRun }: { data: ExperimentPayload; onRun: (id: string) => void }) {
  const document = data.documents.find(item => item.path === data.project_report.path);
  return <><h1 className="title">Report</h1>{data.project_report.exists ? <><div className="kv"><span>{data.project_report.path}</span>{document ? <a className="link" href={`/api/experiments/${data.experiment.experiment_id}/documents?path=${encodeURIComponent(document.path)}&download=1`}>download report.md</a> : null}</div><section className="detail-scroll"><div className="doc"><MarkdownDoc text={data.project_report.text} runs={data.runs} onRun={onRun} /></div></section></> : <p className="empty-copy report-empty">Nothing here yet. The report view fills when an immutable project report is added. Every cited run stays one click from its evidence.</p>}</>;
}

function DocumentView({ data, document, onRun }: { data: ExperimentPayload; document: Document; onRun: (id: string) => void }) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => { setText(null); fetch(`/api/experiments/${data.experiment.experiment_id}/documents?path=${encodeURIComponent(document.path)}`).then(response => response.text()).then(setText); }, [data.experiment.experiment_id, document.path]);
  return <><h1 className="title">{document.title}</h1><div className="kv"><span>{document.kind} <b>{document.path}</b></span><span>{fmtBytes(document.size_bytes)}</span><a className="link" href={`/api/experiments/${data.experiment.experiment_id}/documents?path=${encodeURIComponent(document.path)}&download=1`}>download</a></div><section className="detail-scroll"><div className="doc">{text == null ? <Loading /> : <MarkdownDoc text={text} runs={data.runs} onRun={onRun} />}</div></section></>;
}

function MarkdownDoc({ text, runs, onRun }: { text: string; runs: Run[]; onRun: (id: string) => void }) {
  const linked = useMemo(() => {
    const ids = runs.map(run => run.run_id).sort((a, b) => b.length - a.length);
    return ids.length ? text.replace(new RegExp(`(${ids.map(id => id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g"), "[$1](#run:$1)") : text;
  }, [text, runs]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a({ href, children }) { return href?.startsWith("#run:") ? <button className="cite" onClick={() => onRun(href.slice(5))}>{children}</button> : <a className="link" href={href}>{children}</a>; } }}>{linked}</ReactMarkdown>;
}

function ReviewDock({ token, selection, view, experimentId, onSelect, onView, onToast }: { token: string | null; selection: Selection; view: View; experimentId: string | null; onSelect: (selection: Selection) => void; onView: (view: View) => void; onToast: (message: string) => void }) {
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [notes, setNotes] = useState<DraftNote[]>([]);
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const scope = scopeLabel(selection, view);
  useEffect(() => {
    if (!token) return;
    let active = true;
    const poll = () => api<{ session: ReviewSession }>(`/api/review?token=${encodeURIComponent(token)}`).then(value => active && setSession(value.session)).catch(() => undefined);
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [token]);
  if (!token) return <div id="dock"><div className="dock quiet"><div className="row"><span className="qlink">No agent review session attached · ordinary view is read-only</span></div></div></div>;
  if (!session || session.experiment_id !== experimentId) return <div id="dock"><div className="dock quiet"><div className="row"><span className="qlink">Review capability is unavailable for this experimentation.</span></div></div></div>;
  if (session.status !== "waiting") return <div id="dock"><div className="dock quiet"><div className="row"><span className="qlink">Review session {session.status} · feedback submission is disabled</span></div></div></div>;
  const pending = draft.trim() ? [...notes, { scope, text: draft.trim(), selection, view }] : notes;
  const add = () => { if (!draft.trim()) return; setNotes(pending); setDraft(""); setOpen(false); };
  const submit = (approved: boolean) => {
    const batch = approved ? [...pending, { scope: "review", text: "Approved — continue.", selection: { kind: "overview" } as Selection, view: "bench" as View }] : pending;
    if (!batch.length) return;
    api<{ session: ReviewSession }>("/api/review/submit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token, notes: batch.map(note => ({ scope: note.scope, text: note.text })) }) }).then(value => { setSession(value.session); setNotes([]); setDraft(""); setOpen(false); onToast(approved ? "Approved · agent resumed" : "Follow-up sent · agent resumed with your notes"); });
  };
  return <div id="dock"><div className="dock"><div className="row"><span className="pulse" /><span className="who">agent</span><span className="msg">Agent is waiting for your review. Inspect the evidence and return one scoped batch.</span><span className="sp" /><span className="fine">{scope}</span></div>{notes.length ? <div className="row">{notes.map((note, index) => <span className="note-chip" key={`${note.scope}-${index}`}><button className="scope" onClick={() => { onView(note.view); onSelect(note.selection); }}>{note.scope}</button><span className="txt">{note.text}</span><button className="x" onClick={() => setNotes(notes.filter((_, item) => item !== index))}>×</button></span>)}</div> : null}{open ? <><textarea value={draft} onChange={event => setDraft(event.target.value)} placeholder={`Note on ${scope} — e.g. compare this with the failed parent before deciding.`} /><div className="row"><span className="fine">notes collect here · nothing is sent until you decide</span><span className="sp" /><button className="dbtn" onClick={() => { setOpen(false); setDraft(""); }}>Cancel</button><button className="dbtn primary" disabled={!draft.trim()} onClick={add}>Add note</button></div></> : <div className="row"><button className="dbtn" onClick={() => setOpen(true)}>Add note on “{scope}”</button><span className="sp" /><button className="dbtn" onClick={() => submit(true)}>Approve — continue{notes.length ? ` with ${notes.length} advisory note${notes.length === 1 ? "" : "s"}` : ""}</button><button className="dbtn primary" disabled={!notes.length} onClick={() => submit(false)}>Send follow-up ({notes.length})</button></div>}</div></div>;
}

function scopeLabel(selection: Selection, view: View) {
  if (view === "report") return "project report";
  if (selection.kind === "file") return basename(selection.path);
  if (selection.kind === "managed") return basename(managedPath(selection.key));
  if (selection.kind === "script") return basename(selection.script);
  if (selection.kind === "run") return short(selection.id);
  if (selection.kind === "document") return "document";
  if (selection.kind === "research") return "the loop";
  return "overview";
}

function StatusBar({ data, selection, view, review }: { data: ExperimentPayload | null; selection: Selection; view: View; review: boolean }) {
  let hint = "$ autoexp view";
  if (review) hint = "$ autoexp review · blocking capability attached";
  else if (view === "report") hint = "$ autoexp view · project report";
  else if (selection.kind === "run") hint = `$ autoexp run ${selection.id} · autoexp restore ${selection.id} · autoexp diff ${selection.id}`;
  else if (selection.kind === "research") hint = "$ autoexp research state · autoexp research attempt";
  else if (selection.kind === "file" || selection.kind === "managed") hint = "$ autoexp run · the next run seals current declared inputs";
  else if (data) hint = `$ autoexp status --experiment ${data.experiment.experiment_id}`;
  return <div className="statusbar"><span><b>/</b> filter</span><span><b>↑↓</b>·<b>j k</b> navigate</span><span>workspace stays put; inspector opens beside it</span><span className="right">{hint}</span></div>;
}

function roleShort(role: string): [string, string] {
  if (role === "entrypoint") return ["script", ""];
  if (role === "editable-source") return ["editable", "agent"];
  if (role === "supporting-source") return ["context", "human"];
  if (role === "frozen-evaluator") return ["evaluator", "frozen"];
  if (role === "input-data") return ["data", ""];
  if (role === "secret-source") return ["secrets", ""];
  if (role === "report-guidance") return ["guidance", ""];
  if (role === "generated-output") return ["output", ""];
  return ["file", ""];
}

function Empty({ text }: { text: string }) { return <div className="empty-state">{text}</div>; }
function Loading() { return <div className="loading">Reading recorded evidence…</div>; }

window.addEventListener("autoexp-report", () => document.getElementById("report-toggle")?.click());
export default App;
