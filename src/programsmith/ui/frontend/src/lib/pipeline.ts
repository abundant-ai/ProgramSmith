/*
 * Static metadata describing the pipeline DAG so the UI can render it without
 * the backend re-sending structure each poll. The forward order and node-type
 * coloring mirror the Python FSM (fsm.py) + store.py FORWARD. The pipeline runs
 * fully automatic end to end by default — the two human gates are opt-in.
 */

import type { Stage } from "../api";

export type NodeType = "gate" | "cell" | "sweep" | "decision" | "output";

export interface StageMeta {
  stage: Stage;
  label: string;
  type: NodeType;
  /** Short one-line description shown in the DAG / tooltips. */
  blurb: string;
}

/** Canonical forward DAG order (matches store.py FORWARD). QA_ON_GPT and PR are legacy stages
 *  removed from the flow — they survive only in the Stage type union so old runs still render. */
export const FORWARD_STAGES: StageMeta[] = [
  {
    stage: "INGEST_LOCK",
    label: "Ingest & Lock",
    type: "gate",
    blurb: "Clone, detect license/build, ProgramBench-overlap guard, pin the source SHA.",
  },
  {
    stage: "TASK_MATRIX",
    label: "Task Matrix",
    type: "cell",
    blurb: "Propose candidate tasks and auto-pick the best fit.",
  },
  {
    stage: "ORACLE_GOLDEN",
    label: "Oracle & Golden",
    type: "cell",
    blurb: "Build the sealed oracle pair + docs + Golden-I/O case suite.",
  },
  {
    stage: "CREATE",
    label: "Create",
    type: "cell",
    blurb: "Assemble the ProgramBench-style task via the vendored generator.",
  },
  {
    stage: "SANITY",
    label: "Sanity",
    type: "gate",
    blurb: "Oracle passes, nop fails.",
  },
  {
    stage: "STATIC_CI",
    label: "Static CI",
    type: "gate",
    blurb: "The static check suite must be green.",
  },
  {
    stage: "DIFFICULTY_SWEEP",
    label: "Smoke sweep",
    type: "sweep",
    blurb: "Cheap smoke-model trials — a coarse difficulty read before spending frontier trials.",
  },
  {
    stage: "CALIBRATE",
    label: "Calibrate",
    type: "decision",
    blurb: "Smoke decision — proceed, harden, ease, or flag broken.",
  },
  {
    stage: "QA_PROBE",
    label: "QA Probe",
    type: "decision",
    blurb: "Reward-hack / shortcut detection.",
  },
  {
    stage: "FULL_SWEEP",
    label: "Frontier sweep",
    type: "sweep",
    blurb: "Frontier-model trials — the authoritative 1/3–2/3 difficulty band.",
  },
  {
    stage: "QA_GATE",
    label: "Done",
    type: "output",
    blurb: "Final gate (automatic): accept exports the task; revise/reject loop back.",
  },
];

/** Side / loop node — target of every backward (harden/ease/revise) edge. */
export const SYNTHESIZE_META: StageMeta = {
  stage: "SYNTHESIZE",
  label: "Synthesize",
  type: "cell",
  blurb: "Surgical-patch cell (harden / ease / revise) — rejoin the forward chain after a fix.",
};

export const ALL_STAGE_META: Record<string, StageMeta> = Object.fromEntries(
  [...FORWARD_STAGES, SYNTHESIZE_META].map((m) => [m.stage, m]),
);

export const TERMINAL_STAGES: Stage[] = ["DONE", "DROPPED", "BLOCKED", "EASY_SHELF"];

export interface NodeTypeMeta {
  key: NodeType;
  label: string;
  /** css var token name for the accent color. */
  color: string;
}

export const NODE_TYPES: NodeTypeMeta[] = [
  { key: "gate", label: "Code gate", color: "var(--color-node-gate)" },
  { key: "cell", label: "LLM cell", color: "var(--color-node-cell)" },
  { key: "sweep", label: "Sweep", color: "var(--color-node-sweep)" },
  { key: "decision", label: "Decision", color: "var(--color-node-decision)" },
  { key: "output", label: "Output", color: "var(--color-node-output)" },
];

export function nodeColor(type: NodeType): string {
  return NODE_TYPES.find((n) => n.key === type)?.color ?? "var(--color-accent)";
}

export function stageMeta(stage: string): StageMeta {
  return (
    ALL_STAGE_META[stage] ?? {
      stage: stage as Stage,
      label: stage,
      type: "gate",
      blurb: "",
    }
  );
}

/** Friendly label for any stage (incl. terminal + the legacy stages dropped from the DAG). */
export function stageLabel(stage: string): string {
  const friendly: Record<string, string> = {
    DONE: "Done",
    DROPPED: "Dropped",
    BLOCKED: "Blocked",
    EASY_SHELF: "Easy shelf",
  };
  return friendly[stage] ?? ALL_STAGE_META[stage]?.label ?? stage;
}
