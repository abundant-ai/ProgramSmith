import { motion } from "framer-motion";
import { Check } from "lucide-react";
import type { NodeStatus } from "../api";
import {
  FORWARD_STAGES,
  NODE_TYPES,
  SYNTHESIZE_META,
  nodeColor,
  type StageMeta,
} from "../lib/pipeline";
import { cn } from "../lib/cn";

/* ---- layout geometry (SVG units) -----------------------------------------
 * Serpentine grid of the 11 forward stages (4 per row, rows 0–2), with
 * SYNTHESIZE on a compact bottom row and the harden/ease loops drawn as quiet
 * curves into it. Edges stay silent (muted) until a transition is in progress —
 * then the single active edge lights up and flows. A flowchart, not a signboard.
 */
const COLS = 4;
const NODE_W = 172;
const NODE_H = 50;
const GAP_X = 60;
const GAP_Y = 58; // tightened — the loops are quiet now, so the rows can sit close
const PAD = 24;
const R = 12;

const COL_PITCH = NODE_W + GAP_X;
const ROW_PITCH = NODE_H + GAP_Y;

const WIDTH = PAD + COLS * NODE_W + (COLS - 1) * GAP_X + PAD;

interface Pt {
  x: number;
  y: number;
  cx: number;
  cy: number;
}
function makePt(x: number, y: number): Pt {
  return { x, y, cx: x + NODE_W / 2, cy: y + NODE_H / 2 };
}

/** Serpentine (boustrophedon) position for forward node i. */
function pos(i: number): Pt {
  const row = Math.floor(i / COLS);
  let col = i % COLS;
  if (row % 2 === 1) col = COLS - 1 - col;
  return makePt(PAD + col * COL_PITCH, PAD + row * ROW_PITCH);
}

const IDX = Object.fromEntries(FORWARD_STAGES.map((s, i) => [s.stage, i]));
// SYNTHESIZE lives on its own compact row below the 3 forward rows, left of center so the
// harden/ease fan-in curves have room.
const SYNTH_W = 184;
const SYNTH_POS = makePt(PAD + 0.5 * COL_PITCH, PAD + 3 * ROW_PITCH - 2);
const HEIGHT = SYNTH_POS.y + NODE_H + PAD;

// backward edges (rejoin at SYNTHESIZE): CALIBRATE/QA_PROBE tune the smoke phase, FULL_SWEEP the
// frontier (harden AND ease). The revise edge is intentionally not drawn (kept visually quiet).
const HARDEN_SOURCES = ["CALIBRATE", "QA_PROBE", "FULL_SWEEP"];

function statusOf(statuses: Record<string, NodeStatus>, stage: string): NodeStatus {
  return statuses[stage] ?? "pending";
}

export function PipelineDag({
  statuses,
  onSelectStage,
  selected,
}: {
  statuses: Record<string, NodeStatus>;
  onSelectStage?: (stage: string) => void;
  selected?: string | null;
}) {
  const synthP = SYNTH_POS;
  const synthTopY = synthP.y;
  const fan = (k: number, n: number) => synthP.x + (SYNTH_W * (k + 1)) / (n + 1);
  const synthActive = statusOf(statuses, "SYNTHESIZE") === "current";

  return (
    <div className="w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full min-w-[720px]"
        role="img"
        aria-label="Pipeline DAG"
      >
        <defs>
          <marker
            id="dag-arrow"
            viewBox="0 0 10 10"
            refX="8"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
            markerUnits="userSpaceOnUse"
          >
            <path d="M1,1 L9,5 L1,9 Z" fill="context-stroke" />
          </marker>
        </defs>

        {/* forward edges — three states: a COMPLETED hop (done→done) is a solid accent line; the
            single IN-PROGRESS hop (done→current) lights up and flows; everything ahead is a silent
            hairline. */}
        {FORWARD_STAGES.slice(0, -1).map((_, i) => {
          const from = statusOf(statuses, FORWARD_STAGES[i].stage);
          const to = statusOf(statuses, FORWARD_STAGES[i + 1].stage);
          const state = from === "done" && to === "current" ? "active"
            : from === "done" && to === "done" ? "done"
              : "muted";
          return <ForwardEdge key={`e-${i}`} a={pos(i)} b={pos(i + 1)} state={state} />;
        })}

        {/* harden/ease loops → SYNTHESIZE, then rejoin — quiet unless a patch is in progress */}
        {HARDEN_SOURCES.map((s, k) => (
          <LoopEdge
            key={`h-${s}`}
            from={pos(IDX[s])}
            tx={fan(k, HARDEN_SOURCES.length)}
            ty={synthTopY}
            active={synthActive}
          />
        ))}
        <RejoinEdge from={synthP} to={pos(IDX["STATIC_CI"])} active={synthActive} />

        {/* single quiet loop label */}
        <LoopLabel x={synthP.cx - 34} y={synthTopY - 12} text="harden / ease" active={synthActive} />

        {/* nodes */}
        {FORWARD_STAGES.map((meta, i) => (
          <Node key={meta.stage} meta={meta} p={pos(i)} status={statusOf(statuses, meta.stage)} step={i + 1}
                onSelect={onSelectStage} isSelected={selected === meta.stage} />
        ))}
        <Node meta={SYNTHESIZE_META} p={synthP} status={statusOf(statuses, "SYNTHESIZE")} dashed w={SYNTH_W}
              onSelect={onSelectStage} isSelected={selected === "SYNTHESIZE"} />
      </svg>

      {/* legend */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 px-1">
        {NODE_TYPES.map((t) => (
          <span key={t.key} className="flex items-center gap-1.5 text-[12px] text-ink-4">
            <span className="size-2.5" style={{ background: t.color }} />
            {t.label}
          </span>
        ))}
      </div>
    </div>
  );
}

/* An edge lights up only while its transition is in progress: accent stroke + a flowing dash.
 * Otherwise it is a hairline the eye can ignore. */
const MUTED = "var(--color-line)";
const ACTIVE = "var(--color-accent)";

/* ---- forward edge (orthogonal, arrowhead) -------------------------------- */
function ForwardEdge({ a, b, state }: { a: Pt; b: Pt; state: "active" | "done" | "muted" }) {
  const sameRow = Math.abs(a.y - b.y) < 1;
  let d: string;
  if (sameRow) {
    const ltr = a.x < b.x;
    const sx = ltr ? a.x + NODE_W : a.x;
    const ex = ltr ? b.x : b.x + NODE_W;
    d = `M ${sx} ${a.cy} L ${ex} ${b.cy}`;
  } else {
    const sx = a.cx;
    const sy = a.y + NODE_H;
    const ex = b.cx;
    const ey = b.y;
    if (Math.abs(sx - ex) < 1) {
      d = `M ${sx} ${sy} L ${ex} ${ey}`;
    } else {
      const midY = (sy + ey) / 2;
      const dir = ex > sx ? 1 : -1;
      const r = Math.min(R, Math.abs(ex - sx) / 2, (midY - sy) / 2, (ey - midY) / 2);
      d = [
        `M ${sx} ${sy}`,
        `L ${sx} ${midY - r}`,
        `Q ${sx} ${midY} ${sx + dir * r} ${midY}`,
        `L ${ex - dir * r} ${midY}`,
        `Q ${ex} ${midY} ${ex} ${midY + r}`,
        `L ${ex} ${ey}`,
      ].join(" ");
    }
  }
  if (state === "active") {
    // the one in-progress hop: accent, dashed, flowing
    return (
      <motion.path
        d={d}
        fill="none"
        stroke={ACTIVE}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeDasharray="4 5"
        markerEnd="url(#dag-arrow)"
        animate={{ strokeDashoffset: [0, -18] }}
        transition={{ duration: 0.9, repeat: Infinity, ease: "linear" }}
      />
    );
  }
  // a completed hop is SOLID accent (no dash, no animation); everything ahead is a silent hairline
  const done = state === "done";
  return (
    <path
      d={d}
      fill="none"
      stroke={done ? ACTIVE : MUTED}
      strokeWidth={done ? 1.75 : 1.25}
      strokeLinecap="round"
      strokeLinejoin="round"
      markerEnd="url(#dag-arrow)"
      opacity={done ? 0.9 : 0.5}
    />
  );
}

/* ---- harden/ease loop edge (smooth, dashed) ------------------------------ */
function LoopEdge({ from, tx, ty, active }: { from: Pt; tx: number; ty: number; active: boolean }) {
  const sx = from.cx;
  const sy = from.y + NODE_H;
  const dy = ty - sy;
  const c1x = sx;
  const c1y = sy + dy * 0.5;
  const c2x = tx;
  const c2y = ty - dy * 0.4;
  const d = `M ${sx} ${sy} C ${c1x} ${c1y} ${c2x} ${c2y} ${tx} ${ty}`;
  return (
    <path
      d={d}
      fill="none"
      stroke={active ? ACTIVE : MUTED}
      strokeWidth={active ? 1.75 : 1}
      strokeDasharray="4 5"
      strokeLinecap="round"
      markerEnd="url(#dag-arrow)"
      opacity={active ? 0.9 : 0.28}
    />
  );
}

/* ---- rejoin: SYNTHESIZE → STATIC_CI (the patch goes back into the chain) -- */
function RejoinEdge({ from, to, active }: { from: Pt; to: Pt; active: boolean }) {
  const sx = from.cx + 30;
  const sy = from.y;
  const ex = to.cx;
  const ey = to.y + NODE_H;
  const dy = sy - ey;
  const d = `M ${sx} ${sy} C ${sx} ${sy - dy * 0.45} ${ex} ${ey + dy * 0.45} ${ex} ${ey}`;
  return (
    <path
      d={d}
      fill="none"
      stroke={active ? ACTIVE : MUTED}
      strokeWidth={active ? 1.75 : 1}
      strokeDasharray="4 5"
      strokeLinecap="round"
      markerEnd="url(#dag-arrow)"
      opacity={active ? 0.9 : 0.28}
    />
  );
}

function LoopLabel({ x, y, text, active }: { x: number; y: number; text: string; active: boolean }) {
  return (
    <text
      x={x}
      y={y}
      fontSize="10.5"
      fontWeight={500}
      fill={active ? ACTIVE : "var(--color-ink-4)"}
      opacity={active ? 0.95 : 0.6}
    >
      {text}
    </text>
  );
}

/* ---- node -------------------------------------------------------------- */
function Node({
  meta,
  p,
  status,
  step,
  dashed,
  w = NODE_W,
  onSelect,
  isSelected,
}: {
  meta: StageMeta;
  p: Pt;
  status: NodeStatus;
  step?: number;
  dashed?: boolean;
  w?: number;
  onSelect?: (stage: string) => void;
  isSelected?: boolean;
}) {
  const color = nodeColor(meta.type);
  const isCurrent = status === "current";
  const isDone = status === "done";
  const isPending = status === "pending";
  const tint = isCurrent ? 0.16 : isDone ? 0.09 : 0.04;
  const border = isSelected
    ? "var(--color-accent)"
    : isCurrent || isDone
      ? color
      : "var(--color-line)";

  return (
    <g
      transform={`translate(${p.x}, ${p.y})`}
      className={cn(onSelect ? "cursor-pointer" : "cursor-default")}
      onClick={onSelect ? () => onSelect(meta.stage) : undefined}
    >
      <title>{`${meta.label} — ${meta.blurb}${onSelect ? " (click to inspect)" : ""}`}</title>
      {/* base + type tint */}
      <rect
        width={w}
        height={NODE_H}
        rx={0}
        fill="var(--color-surface-2)"
        stroke={border}
        strokeWidth={isSelected || isCurrent ? 2 : 1.25}
        strokeDasharray={dashed ? "5 4" : undefined}
      />
      <rect width={w} height={NODE_H} rx={0} fill={color} fillOpacity={tint} />

      {/* status chip top-right */}
      <g transform={`translate(${w - 24}, 11)`}>
        {isDone ? (
          <>
            <circle r={8} cx={5} cy={5} fill={color} fillOpacity={0.9} />
            <Check x={0} y={0} width={10} height={10} stroke="var(--color-surface)" strokeWidth={2.4} />
          </>
        ) : isCurrent ? (
          <motion.circle
            r={4.5}
            cx={5}
            cy={5}
            fill={color}
            animate={{ opacity: [1, 0.35, 1] }}
            transition={{ duration: 1.5, repeat: Infinity }}
          />
        ) : (
          <circle r={4} cx={5} cy={5} fill="none" stroke="var(--color-line-2)" strokeWidth={1.5} />
        )}
      </g>

      {step !== undefined && (
        <text x={14} y={19} fontSize="10.5" fontWeight={600} fill="var(--color-ink-4)">
          {String(step).padStart(2, "0")}
        </text>
      )}
      <text
        x={14}
        y={step !== undefined ? 37 : 30}
        fontSize="13"
        fontWeight={600}
        fill={isPending ? "var(--color-ink-3)" : "var(--color-ink)"}
      >
        {meta.label}
      </text>
    </g>
  );
}
