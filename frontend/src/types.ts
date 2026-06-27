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
