export type Experiment = {
  experiment_id: string;
  repo_id: string;
  repo_title: string;
  repo_path: string;
  title: string;
  objective: string;
  kind: "standard" | "autoresearch";
  status: string;
  runner: string;
  created_at: string;
  updated_at: string;
  exists: boolean;
  run_count?: number;
  latest_run_id?: string;
  latest_run_status?: string;
  latest_run_at?: string;
};

export type Repository = {
  repo_id: string;
  title: string;
  path: string;
  exists: boolean;
  experiments: Experiment[];
};

export type ManifestFile = {
  experiment_id: string;
  path: string;
  role: string;
  description: string;
  content_hash: string | null;
  available: boolean;
  secret_keys: Array<{ name: string; populated: boolean }>;
};

export type Run = {
  run_id: string;
  title?: string;
  status: string;
  script_name: string;
  created_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  report_path?: string;
  duration_ms?: number | null;
  exit_code?: number | null;
  source_snapshot_id?: string | null;
  parent_run_id?: string | null;
  capsule_hash?: string;
  output_hash?: string;
  runner?: string;
  runner_identity?: string;
  failure_message?: string | null;
  changes?: string[];
};

export type Artifact = {
  artifact_id: string;
  run_id: string;
  category: "output" | "log" | "report";
  path: string;
  media_type: string;
  size_bytes: number;
  content_hash: string;
  content_url: string;
  metadata: Record<string, unknown>;
};

export type Document = {
  document_id: string;
  experiment_id: string;
  run_id?: string | null;
  kind: "report" | "insight";
  title: string;
  path: string;
  size_bytes: number;
  created_at: string;
};

export type ResearchAttempt = {
  key: string;
  attempt_id: string;
  sequence: number;
  hypothesis: string;
  status: string;
  state: string;
  verdict?: "kept" | "reverted" | null;
  score?: number | null;
  improvement?: number | null;
  run_id?: string | null;
  created_at?: string;
};

export type ResearchState = {
  objective: { metric: string; direction: "min" | "max"; baseline: number | null; best: number | null; budget_sec: number; current_best_snapshot_id?: string | null };
  contract: { contract_id: string; status: string; evaluator_hash?: string; subject_path?: string; evaluator_path?: string; program_path?: string };
  experiments: ResearchAttempt[];
  files: Array<{ path: string; role: string; desc: string; hash?: string }>;
  loop: { active: boolean; phase: string; status: string; failure_message?: string | null };
};

export type ExperimentPayload = {
  experiment: Experiment;
  files: ManifestFile[];
  runs: Run[];
  documents: Document[];
  milestones: Array<{
    milestone_id: string;
    target_kind: "run" | "attempt";
    target_id: string;
    title: string;
    significance: string;
    created_at: string;
  }>;
  project_report: { path: string; text: string; exists: boolean };
  managed: {
    stage: Record<string, unknown>;
    params: Record<string, unknown>;
    params_schema: Record<string, unknown>;
    report_guidance: string;
  };
  research?: ResearchState;
};

export type RunOverview = {
  run: Run;
  source_snapshot?: Record<string, unknown> | null;
  parent_run?: Run | null;
  trigger?: Record<string, unknown> | null;
  reproducibility?: Record<string, unknown> | null;
  artifact_summary?: { total: number; output: number; log: number; report: number; artifacts: Artifact[] };
  reproduction?: Record<string, unknown> | null;
};

export type ReviewSession = {
  session_id: string;
  experiment_id: string;
  status: "waiting" | "completed" | "expired";
  expires_at: number;
  notes: Array<{ scope: string; text: string }>;
};
