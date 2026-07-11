export type Run = {
  run_id: string;
  status: string;
  script_name?: string;
  script?: string;
  report_path?: string;
  output_files?: string[];
  created_at?: string;
  source_snapshot_id?: string | null;
  parent_run_id?: string | null;
  trigger_id?: string | null;
  exit_code?: number | null;
  runner?: string | null;
  runner_identity?: string | null;
  capsule_hash?: string | null;
  output_hash?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  reproduces_run_id?: string | null;
  failure_kind?: string | null;
  failure_message?: string | null;
};

export type RunTrigger = {
  trigger_id?: string;
  kind?: string;
  actor?: string;
  actor_name?: string;
  session_id?: string | null;
  request_id?: string | null;
};

export type ReproducibilitySummary = {
  state?: string;
  checks?: Record<string, unknown>;
  external_inputs?: unknown[];
  [key: string]: unknown;
};

export type ArtifactSummary = {
  total?: number;
  output?: number;
  outputs?: number;
  log?: number;
  logs?: number;
  report?: number;
  reports?: number;
};

export type RunDetail = Run & {
  trigger?: RunTrigger | string | null;
  source_snapshot?: SourceSnapshot | null;
  parent_run?: Run | null;
  reproducibility?: ReproducibilitySummary | null;
  artifact_summary?: ArtifactSummary | null;
  divergence?: boolean;
  reproduction?: string | Record<string, unknown> | null;
};

export type RunAggregate = {
  run: RunDetail;
  source_snapshot?: SourceSnapshot | null;
  parent_run?: Run | null;
  trigger?: RunTrigger | string | null;
  reproducibility?: ReproducibilitySummary | null;
  artifact_summary?: ArtifactSummary | null;
  reproduction?: string | Record<string, unknown> | null;
  divergence?: boolean;
};

export type RunDetailPayload = RunDetail | RunAggregate;

export type Artifact = {
  artifact_id: string;
  run_id?: string;
  category?: "output" | "log" | "report" | string;
  path: string;
  media_type?: string | null;
  content_hash?: string | null;
  size_bytes?: number | null;
  metadata?: Record<string, unknown> | null;
  content_url?: string;
};

export type ArtifactListPayload = Artifact[] | { artifacts: Artifact[] };

export type ArtifactDetailPayload = {
  artifact?: Artifact;
  kind?: string;
  text?: string;
  content?: string;
  json?: unknown;
  preview?: unknown;
  columns?: string[];
  rows?: unknown[][] | Record<string, unknown>[];
  truncated?: boolean;
  data_url?: string;
  content_url?: string;
  metadata?: Record<string, unknown>;
};

export type RunLogPayload = {
  stream: "stdout" | "stderr" | string;
  text: string;
  next_offset: number;
  terminal: boolean;
};

export type RunDiffPayload = {
  summary?: Record<string, unknown> | Array<Record<string, unknown>>;
  changes?: Record<string, unknown>;
  changed_files?: string[];
  diff?: string;
  unified_diff?: string;
};

export type Project = {
  project_id: string;
  title: string;
  path: string;
  exists: boolean;
  last_opened_at?: string;
  runner?: "docker" | "local" | string;
  mode?: "standard" | "autoresearch" | string;
};

export type SourceFile = {
  path: string;
  text: string;
};

export type SourcePayload = {
  run_id: string;
  script?: string;
  selected: string;
  files: SourceFile[];
};

export type ReportPayload = {
  run_id: string;
  path: string;
  text: string;
};

export type InstructionPayload = {
  source: string;
  text: string;
};

export type ParamsPayload = {
  schema: Record<string, unknown> | null;
  params: Record<string, unknown> | null;
  snapshot?: SourceSnapshot;
};

export type SourceSnapshot = {
  snapshot_id: string;
  parent_snapshot_id?: string | null;
  git_commit: string;
  source_hash: string;
};

export type ScriptSavePayload = {
  path: string;
  snapshot: SourceSnapshot;
};

export type ResearchObjective = {
  metric: string;
  direction: "min" | "max";
  baseline: number | null;
  best: number | null;
  budget_sec: number;
};

export type ResearchFile = {
  path: string;
  role: "human" | "agent" | "frozen";
  desc: string;
  hash?: string;
};

export type ResearchContract = {
  contract_id: string;
  status: string;
  current_best_snapshot_id: string | null;
  evaluator_hash: string | null;
};

export type ResearchPreflight = {
  ok: boolean;
  checks: Array<{
    name: string;
    ok: boolean;
    detail: string;
    required: boolean;
  }>;
};

export type ResearchExperiment = {
  key?: string;
  attempt_id?: string;
  tag?: string;
  contract_id?: string;
  state?: "running" | "scored" | "failed";
  verdict?: "kept" | "reverted" | null;
  status?: "running" | "kept" | "reverted" | "failed";
  score: number | null;
  hypothesis?: string;
  hyp?: string;
  base_snapshot_id?: string | null;
  candidate_snapshot_id?: string | null;
  best_score_before?: number | null;
  improvement?: number | null;
  created_at?: string;
  commit?: string | null;
  has_diff?: boolean;
  diff?: string | null;
  run_id?: string | null;
};

export type ResearchState = {
  contract: ResearchContract;
  objective: ResearchObjective;
  preflight: ResearchPreflight;
  files: ResearchFile[];
  experiments: ResearchExperiment[];
  loop: {
    active: boolean;
    phase: string;
    tag: string | null;
    session_id?: string | null;
    status?: string;
    failure_message?: string | null;
  };
  can_import_baseline: boolean;
};

export type ResearchFilePayload = {
  path: string;
  text: string;
  role: ResearchFile["role"];
  hash?: string;
  snapshot?: SourceSnapshot;
};
