export type Run = {
  run_id: string;
  status: string;
  script_name?: string;
  script?: string;
  report_path?: string;
  output_files?: string[];
  created_at?: string;
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

export type ResearchExperiment = {
  tag: string;
  status: "running" | "kept" | "reverted";
  score: number | null;
  hyp: string;
  commit?: string | null;
  has_diff?: boolean;
  diff?: string | null;
  run_id?: string;
};

export type ResearchState = {
  objective: ResearchObjective;
  files: ResearchFile[];
  experiments: ResearchExperiment[];
  loop: { active: boolean; phase: string; tag: string | null };
};

export type ResearchFilePayload = {
  path: string;
  text: string;
  role: ResearchFile["role"];
  hash?: string;
};
