/*
 * Typed client for the ProgramSmith JSON API (served same-origin under /api).
 * Every shape here mirrors the FastAPI contract in the backend's ui/api.py.
 */

// ---- domain types -------------------------------------------------------------

export type Stage =
  | "INGEST_LOCK"
  | "TASK_MATRIX"
  | "ORACLE_GOLDEN"
  | "CREATE"
  | "SANITY"
  | "STATIC_CI"
  | "DIFFICULTY_SWEEP" // smoke sweep (cheap smoke model) — enum value kept stable for persisted state
  | "CALIBRATE"
  | "QA_PROBE"
  | "FULL_SWEEP" // frontier sweep (frontier model)
  | "QA_ON_GPT" // LEGACY: removed from the flow (ADR-0039) but kept so old runs still render
  | "QA_GATE"
  | "SYNTHESIZE"
  | "PR" // LEGACY: removed from the flow (ADR-0039) but kept so old runs still render
  | "DONE"
  | "DROPPED"
  | "BLOCKED"
  | "EASY_SHELF"; // terminal: good task, frontier-saturated — shelved, not trashed (ADR-0040)

export type RunStatus =
  | "in_progress"
  | "draft"
  | "done"
  | "dropped"
  | "blocked"
  | "easy" // EASY_SHELF terminal (ADR-0040)
  | string;

export type NodeStatus = "done" | "current" | "pending";

/** A long-running server-side job tracked per run. */
export type JobStatus = "running" | "done" | "error";
export interface JobState {
  status: JobStatus;
  detail?: string | null;
}
/** Background jobs surfaced on a run detail. An absent key = not started. Keys also include the
 *  agentic cells: oracle-generate, create-fill, synthesize-h{n}-r{n}. */
export interface RunJobs {
  ingest?: JobState;
  task_matrix?: JobState;
  [name: string]: JobState | undefined;
}

export interface PreflightCheck {
  name: string;
  ok: boolean;
  detail: string;
}
export interface Preflight {
  ready: boolean;
  checks: PreflightCheck[];
}

export interface Settings {
  github_user: string | null;
  default_cell_model: string | null;
  runs_dir: string | null;
  ci_repo_root: string | null;
  difficulty_trials: number | null;
  full_trials: number | null;
  harden_drop_after: number | null;
  harden_min_improvement: number | null;
  agentic_concurrency: number | null;
  // ---- both human gates optional, default AUTO (zero-touch farm runs) ----
  task_matrix_mode?: "auto" | "human" | string | null;
  qa_gate_mode?: "auto" | "human" | string | null;
  // Accepted tasks export to <outbox_dir>/tasks/, easy → /easy/.
  outbox_dir?: string | null;
  // Cell model routing: heavy = default_cell_model, light one-shots, trajectory audits.
  cell_model_light?: string | null;
  cell_model_analysis?: string | null;
  // Task authorship, stamped into generated task.toml.
  author_name?: string | null;
  author_email?: string | null;
  author_organization?: string | null;
  // Secrets are returned masked (presence + last four characters), never raw.
  claude_code_oauth_token?: string | null;
  anthropic_api_key?: string | null;
  openai_api_key?: string | null;
  gemini_api_key?: string | null;
  zai_api_key?: string | null;
}

/** Effective runtime the served auto-driver is using (read-only status). */
export interface RuntimeInfo {
  autodrive: boolean;
  spend: boolean;
  agentic: boolean;
  interval_sec: number;
  runs_dir: string;
  ci_repo_root: string | null;
  ci_repo_ok: boolean;
  difficulty_trials: number;
  full_trials: number;
  harden_drop_after: number;
  harden_min_improvement: number;
  agentic_concurrency: number;
  /** Gate modes, when the backend surfaces them on /runtime. */
  task_matrix_mode?: string;
  qa_gate_mode?: string;
}

export interface CostTotals {
  usd: number;
  sessions: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  duration_ms: number;
}
export interface CostRun extends CostTotals { run: string }
export interface CostEvent {
  id: string;
  ts: string;
  run: string;
  stage: string;
  model: string | null;
  usd: number;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
}
export interface CostsResponse {
  totals: CostTotals;
  by_run: CostRun[];
  recent: CostEvent[];
}

// ---- per-run sweep config (New Run → Advanced options) ------------------------
export interface AgentSpec {
  harness: string;
  model: string;
  n_trials: number;
}
/** Per-model acceptance band — used with the `any`/`all` combinators. `basis` is a harness key. */
export interface ModelBand {
  basis: string;
  min_pass: number;
  max_pass: number;
}
export interface BandSpec {
  basis: string; // "aggregate" | a harness key
  min_pass: number; // 0..1
  max_pass: number; // 0..1 — saturation ceiling
  /** "aggregate" (default/legacy) | "any" | "all" — how per-model bands combine. */
  combinator?: string;
  /** Per-model acceptance bands, applied when combinator is "any" or "all". */
  per_model?: ModelBand[];
}
export interface StageSpec {
  agents: AgentSpec[];
  band: BandSpec;
}
export interface RunConfig {
  difficulty: StageSpec;
  full: StageSpec;
}
export interface CatalogEntry {
  label: string;
  provider: string;
  recommended?: boolean;
}
export interface Catalog {
  harnesses: Record<string, CatalogEntry>;
  models: Record<string, CatalogEntry>;
  default_config: RunConfig;
}

export interface RunSummary {
  key: string;
  run_id: string;
  slug: string | null;
  stage: Stage;
  status: RunStatus;
  paused: boolean;
  harden: number;
  revise: number;
  /** Ease (make-easier) tuning rounds (ADR-0040) — optional so pre-pivot backends still parse. */
  ease?: number;
  difficulty_pass_at_1: string | null;
  /** Compact full-sweep band, e.g. "cc=100% cx=20%", once measured. */
  full_sweep_band: string | null;
  /** 0..1 fraction of the forward DAG reached (furthest stage; correct even while looping). */
  progress: number;
  awaiting_human: boolean;
  /** A slow op running server-side in the background, if any (ingest | task_matrix |
   *  oracle-generate | create-fill | synthesize-h{n}-r{n}). */
  active_job: string | null;
  /** Operationally halted (not a terminal FSM state): waiting on env/spend/missing-input/error. */
  blocked: boolean;
  /** Benign in-flight wait: polling a launched sweep (or within bounded retry). Shown as
   *  "waiting", not the red "blocked" badge. Mutually exclusive with `blocked`. */
  waiting?: boolean;
  /** Why the last drive halted — shown so a stuck run explains itself. */
  halt_reason: string | null;
  /** DEPRECATED (ADR-0039: the PR stage is gone). The backend still emits it alongside `exported`
   *  for old-frontend compatibility; treat it as a synonym for exported. */
  ready_for_pr?: boolean;
  /** DONE + task bundle copied to the outbox — the success terminal (replaces ready_for_pr). */
  exported?: boolean;
  /** The operator who created the run (email), when the fleet is authed (WS5). */
  created_by?: string | null;
  /** Repository rejected before task generation; distinct from a downstream task failure. */
  screened_out?: boolean;
  /** TASK MATRIX selected a candidate; this run is now a task rather than a source lead. */
  source_admitted?: boolean;
}

export interface FleetCounters {
  total: number;
  sourced?: number;
  screening?: number;
  admitted?: number;
  in_progress: number;
  /** Static-CI-complete outputs intentionally created without calibration sweeps. */
  drafts?: number;
  accepted: number;
  exported?: number;
  dropped: number;
  screened_out?: number;
  blocked: number;
  paused: number;
  /** Runs shelved at EASY_SHELF (ADR-0040) — optional so pre-pivot backends still parse. */
  easy?: number;
}

export interface RunsResponse {
  runs: RunSummary[];
  counters: FleetCounters;
}

export interface StageEvent {
  stage: Stage;
  verdict: string;
  next: Stage;
  reason: string;
  ts: string | null;
}

export interface SourceInfo {
  repo: string;
  pinned_sha: string;
  repo_url: string | null;
  license: string | null;
  license_class: string;
  copyleft_blocked: boolean;
  primary_language: string | null;
  build_systems: string[];
  test_frameworks: string[];
  size_files: number | null;
  size_loc: number | null;
  clone_path: string | null;
  has_cli_entrypoint?: boolean | null;
  cli_entrypoint?: string | null;
}

export interface SourceScreenResult {
  profile: "full" | "draft";
  eligible: boolean;
  reason: string;
  checks: Record<string, unknown>;
  warnings?: string[];
}

export interface Dimensions {
  // legacy rewrite-port axes (old manifests)
  target_language: string | null;
  scope_unit: string | null;
  verifier_mechanism: string | null;
  objective: string | null;
  // ProgramBench axes (ADR-0038) — optional so old manifests still parse
  tool_name?: string | null;
  binary_name?: string | null;
  upstream_language?: string | null;
  flag_surface?: string | null;
}

/** One saturated harden generation, recorded by the harden-review auditor. */
export interface HardenGeneration {
  stage?: string;
  generation?: number;
  pass_at_1?: number | null;
  verdict?: "harden" | "drop" | string;
}

export interface RunContext {
  source?: SourceInfo | null;
  dimensions?: Dimensions | null;
  oracle?: Record<string, unknown> | null;
  sweeps?: Record<string, unknown> | null;
  harden_history?: HardenGeneration[];
  run_config?: RunConfig | null;
  task_brief?: string | null;
  source_screen?: SourceScreenResult | null;
}

export interface DriveStep {
  stage: Stage;
  verdict: string;
  next: Stage;
  reason: string;
}
export interface DriveResult {
  steps: DriveStep[];
  final_stage: Stage;
  final_status: RunStatus;
  halted: "human" | "paused" | "terminal" | "blocked" | "max_steps" | string;
  halt_reason: string;
}

/** Read-only "what is this run waiting on?" — computed without side effects (no spend/Docker). */
export interface WaitingInfo {
  stage: Stage;
  kind: "terminal" | "paused" | "human" | "runnable" | "blocked" | string;
  reason: string;
  /** Terminal runs only: true when the operator can re-open it for another harden attempt. */
  can_reopen?: boolean;
}

export interface RunDetail {
  summary: RunSummary;
  node_statuses: Record<string, NodeStatus>;
  history: StageEvent[];
  context: RunContext;
  drive?: DriveResult | null;
  /** What the current stage is waiting on, shown proactively (no Advance click needed). */
  waiting?: WaitingInfo | null;
  /** Background-job state for slow ops (clone/ingest, task-matrix cell). */
  jobs?: RunJobs;
}

/** One TASK MATRIX candidate. The ProgramBench pivot (ADR-0038) replaced the rewrite-port axes
 *  with tool/flag-surface fields; the old fields stay optional so pre-pivot task_matrix.json
 *  files still render. Mirrors cells/task_matrix.py TaskCandidate. */
export interface TaskCandidate {
  // ---- ProgramBench schema (current) ----
  tool_name?: string; // binary name agents must reimplement
  binary_name?: string; // actual executable name (e.g. difft for difftastic)
  upstream_language?: "go" | "rust" | "c" | "cpp" | string;
  flag_surface?: string; // which subcommands/flags the grader will exercise (scope)
  case_families?: string[]; // 5-12 feature families the case suite must cover
  est_kloc?: number;
  stdin_friendly?: boolean;
  needs_files_dir?: boolean;
  deterministic_output?: boolean;
  expert_hours?: number; // full: 12-60; draft: simpler uncalibrated tasks are allowed
  // ---- legacy rewrite-port schema (optional; old runs only) ----
  target_language?: "Rust" | "TypeScript" | string;
  scope_unit?: "whole-library" | "subsystem" | "single-algorithm" | string;
  scope_detail?: string;
  verifier_mechanism?: "golden-io" | "differential-oracle" | string;
  objective?:
    | "equivalence"
    | "equivalence+performance"
    | "equivalence+constraints"
    | string;
  license_ok?: boolean;
  // ---- shared ----
  expected_difficulty: "trivial" | "moderate" | "hard" | "frontier" | string;
  recommendation: "recommended" | "viable" | "marginal" | string;
  rationale: string;
  basis_ref: string;
}

export interface TaskMatrixOutput {
  source_ref: string;
  profile?: "full" | "draft";
  candidates: TaskCandidate[];
  no_candidate_reason?: string | null;
  source_evidence?: string[];
}

export interface CreateRunResult {
  key: string;
  /** Returns immediately; clone+ingest then runs server-side in the background. */
  status: RunStatus;
  stage: Stage;
  // legacy/optional fields retained for compatibility
  verdict?: string;
  reason?: string;
  detail?: string;
}

/** POST /runs/{key}/task-matrix now returns immediately; the cell runs in the background. */
export interface TaskMatrixJobResult {
  status: JobStatus;
}

// ---- file / directory browser (task-detail viewer) -----------------------------

export interface FileNode {
  name: string;
  path: string;
  type: "dir" | "file";
  size?: number;
  lang?: string | null;
  children?: FileNode[];
  truncated?: boolean;
  missing?: boolean;
}
export interface FileTreeResponse {
  root: string;
  tree: FileNode;
}
export type FilePreviewKind = "text" | "image" | "binary" | "too_large";
export interface FilePreview {
  kind: FilePreviewKind;
  name: string;
  path: string;
  size: number;
  lang?: string | null;
  content?: string; // kind === "text"
  data_uri?: string; // kind === "image"
}

// ---- transport ----------------------------------------------------------------

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { json?: unknown },
): Promise<T> {
  const { json, ...rest } = init ?? {};
  const headers = new Headers(rest.headers);
  if (json !== undefined) headers.set("Content-Type", "application/json");

  const res = await fetch(`/api${path}`, {
    ...rest,
    headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });

  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      detail =
        (typeof body?.detail === "string" && body.detail) ||
        (typeof body?.message === "string" && body.message) ||
        JSON.stringify(body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- endpoints ----------------------------------------------------------------

export const api = {
  preflight: () => request<Preflight>("/preflight"),

  getSettings: () => request<Settings>("/settings"),
  saveSettings: (body: Partial<Settings>) =>
    request<Settings>("/settings", { method: "POST", json: body }),
  runtime: () => request<RuntimeInfo>("/runtime"),
  costs: () => request<CostsResponse>("/costs"),

  catalog: () => request<Catalog>("/catalog"),
  listPresets: () => request<{ presets: Record<string, RunConfig> }>("/presets"),
  savePreset: (name: string, config: RunConfig) =>
    request<{ presets: Record<string, RunConfig> }>("/presets", {
      method: "POST",
      json: { name, config },
    }),
  deletePreset: (name: string) =>
    request<{ presets: Record<string, RunConfig> }>(
      `/presets/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),

  listRuns: () => request<RunsResponse>("/runs"),
  createRun: (body: {
    repo: string;
    sha?: string;
    slug?: string;
    brief?: string;
    config?: RunConfig;
    mode?: "full" | "draft";
    cell_model?: string;
  }) => request<CreateRunResult>("/runs", { method: "POST", json: body }),

  getRun: (key: string) =>
    request<RunDetail>(`/runs/${encodeURIComponent(key)}`),

  listFiles: (key: string, path = "") =>
    request<FileTreeResponse>(
      `/runs/${encodeURIComponent(key)}/files${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  readFile: (key: string, path: string) =>
    request<FilePreview>(
      `/runs/${encodeURIComponent(key)}/file?path=${encodeURIComponent(path)}`,
    ),

  runTaskMatrix: (key: string) =>
    request<TaskMatrixJobResult>(
      `/runs/${encodeURIComponent(key)}/task-matrix`,
      { method: "POST" },
    ),
  getTaskMatrix: (key: string) =>
    request<TaskMatrixOutput>(`/runs/${encodeURIComponent(key)}/task-matrix`),

  select: (key: string, pick: number | null) =>
    request<{ stage: Stage; status?: string }>(
      `/runs/${encodeURIComponent(key)}/select`,
      { method: "POST", json: { pick } },
    ),

  // Only meaningful when qa_gate_mode === "human"; the backend 409s in auto mode (ADR-0039).
  qaGate: (key: string, decision: "accept" | "revise" | "reject") =>
    request<{ stage: Stage; status: string; reason: string }>(
      `/runs/${encodeURIComponent(key)}/qa-gate`,
      { method: "POST", json: { decision } },
    ),

  pause: (key: string) =>
    request<{ paused: boolean; halted?: boolean }>(
      `/runs/${encodeURIComponent(key)}/pause`,
      { method: "POST" },
    ),
  resume: (key: string) =>
    request<{ paused: boolean }>(`/runs/${encodeURIComponent(key)}/resume`, {
      method: "POST",
    }),

  reopen: (key: string) =>
    request<{ stage: Stage; status: RunStatus; harden: number }>(
      `/runs/${encodeURIComponent(key)}/reopen`,
      { method: "POST" },
    ),

  // Clear errored/orphaned agentic jobs so a run parked at a blocked stage relaunches them fresh
  // (after the bounded auto-retry is exhausted and the cause is fixed). Returns the cleared job names.
  retry: (key: string) =>
    request<{ cleared: string[] }>(
      `/runs/${encodeURIComponent(key)}/retry`,
      { method: "POST" },
    ),

  // Delete a run ENTIRELY (control-plane state + working tree). Irreversible — confirm before calling.
  deleteRun: (key: string) =>
    request<{ deleted: string }>(
      `/runs/${encodeURIComponent(key)}`,
      { method: "DELETE" },
    ),

  agentOutput: (key: string) =>
    request<AgentOutput>(`/runs/${encodeURIComponent(key)}/agent-output`),
};

export interface AgentOutput {
  exists: boolean;
  running: boolean;
  active_job: string | null;
  tail: string;
  /** Seconds the live agent has been running (null when idle). */
  elapsed_sec: number | null;
  /** True when the agent has run past the slow threshold — likely provider rate-limiting. */
  slow: boolean;
}
