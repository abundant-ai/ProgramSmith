import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  BookMarked,
  Check,
  CircleDot,
  FlaskConical,
  Gauge,
  Loader2,
  Play,
  RotateCcw,
  Target,
  XCircle,
} from "lucide-react";
import {
  api,
  ApiError,
  type RunJobs,
  type TaskCandidate,
  type TaskMatrixOutput,
} from "../api";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";
import { Button } from "./ui/Button";
import { Badge } from "./ui/Badge";
import { cn } from "../lib/cn";
import { titleCase } from "../lib/format";

const DIFFICULTY_TONE: Record<string, string> = {
  trivial: "text-ink-3",
  moderate: "text-info",
  hard: "text-warn",
  frontier: "text-danger",
};
const REC_TONE: Record<string, "ok" | "info" | "warn" | "neutral"> = {
  recommended: "ok",
  viable: "info",
  marginal: "warn",
};

export function TaskMatrixReview({
  runKey,
  jobs,
  manual = true,
  onAdvanced,
}: {
  runKey: string;
  /** Background-job state from the polled run detail (source of truth). */
  jobs?: RunJobs;
  /** True when this is a real human gate (task_matrix_mode=human / --review). When false the
   *  gate is AUTO: the driver runs the cell and picks the best candidate itself, so the panel
   *  shows an informational auto state rather than a blocking "select one" prompt (manual
   *  selection stays available as a fallback for a view-only dashboard with no active driver). */
  manual?: boolean;
  onAdvanced: () => void;
}) {
  const [matrix, setMatrix] = useState<TaskMatrixOutput | null>(null);
  const [selecting, setSelecting] = useState<number | "drop" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  /** Optimistic flag: a just-fired POST before the next poll reflects it. */
  const [posting, setPosting] = useState(false);

  const job = jobs?.task_matrix;
  // Derive the high-level phase from the polled job + whether we've fetched
  // candidates. The run-detail poll keeps this accurate across navigation.
  const jobRunning = job?.status === "running" || posting;
  const jobError = job?.status === "error" ? job.detail ?? "TASK MATRIX failed." : null;

  // Fetch candidates whenever the job is done (or unknown but candidates may
  // already exist from a prior run). Idempotent GET; 404 = not produced yet.
  const fetchedFor = useRef<string | null>(null);
  useEffect(() => {
    // Re-fetch when the run key changes or the job transitions to done.
    const wantFetch =
      job?.status === "done" || (job === undefined && matrix === null);
    if (!wantFetch) return;
    const token = `${runKey}:${job?.status ?? "init"}`;
    if (fetchedFor.current === token) return;
    fetchedFor.current = token;

    let active = true;
    api
      .getTaskMatrix(runKey)
      .then((m) => active && setMatrix(m))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 404) return; // not produced yet
      });
    return () => {
      active = false;
    };
  }, [runKey, job?.status, matrix]);

  // When a fresh job starts running, clear any optimistic flag once the poll
  // catches up, and drop stale candidates so we show the spinner.
  useEffect(() => {
    if (job?.status === "running") {
      setPosting(false);
    }
  }, [job?.status]);

  async function runMatrix() {
    setError(null);
    setMatrix(null);
    setPosting(true); // optimistic — poll will confirm "running"
    fetchedFor.current = null;
    try {
      // Returns immediately; the LLM cell runs in the background.
      await api.runTaskMatrix(runKey);
      onAdvanced(); // nudge the run-detail poll to pick up jobs.task_matrix
    } catch (e) {
      setPosting(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function select(pick: number | null) {
    setSelecting(pick === null ? "drop" : pick);
    setError(null);
    try {
      await api.select(runKey, pick);
      onAdvanced();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSelecting(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <FlaskConical className="size-4 text-ink-3" />
          Task Matrix
        </CardTitle>
        <Badge tone={manual ? "neutral" : "info"}>
          <CircleDot className="size-3" />
          {manual ? "Awaiting selection" : "Auto-select"}
        </Badge>
      </CardHeader>
      <CardBody className="space-y-5">
        {matrix ? (
          <>
            <div className="flex items-center justify-between gap-3">
              <p className="text-[13px] text-ink-3">
                <span className="text-ink-2">{matrix.candidates.length}</span>{" "}
                candidate{matrix.candidates.length === 1 ? "" : "s"} for{" "}
                <span className="font-mono text-ink-2">{matrix.source_ref}</span>
              </p>
              {manual && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void runMatrix()}
                  loading={jobRunning}
                >
                  Regenerate
                </Button>
              )}
            </div>

            <div className="grid gap-3">
              {matrix.candidates.map((c, i) => (
                <CandidateCard
                  key={i}
                  candidate={c}
                  index={i}
                  selected={picked === i}
                  onSelect={() => setPicked(i)}
                />
              ))}
            </div>

            {!!matrix.source_evidence?.length && (
              <div className="border border-line bg-surface-2 px-3 py-2.5">
                <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-ink-4">
                  Source evidence
                </div>
                <ul className="mt-2 space-y-1 text-[12.5px] text-ink-2">
                  {matrix.source_evidence.map((evidence) => (
                    <li key={evidence}>• {evidence}</li>
                  ))}
                </ul>
              </div>
            )}

            {error && <ErrorBox>{error}</ErrorBox>}

            {manual ? (
              <div className="flex items-center justify-between gap-3 border-t border-line pt-4">
                <Button
                  variant="danger"
                  onClick={() => void select(null)}
                  loading={selecting === "drop"}
                >
                  <XCircle className="size-4" />
                  Drop (none)
                </Button>
                <Button
                  variant="primary"
                  disabled={picked === null}
                  onClick={() => picked !== null && void select(picked)}
                  loading={typeof selecting === "number"}
                >
                  <Check className="size-4" />
                  Select candidate{picked !== null ? ` #${picked + 1}` : ""}
                </Button>
              </div>
            ) : (
              <p className="border-t border-line pt-4 text-[13px] text-ink-3">
                The driver selects the best candidate automatically — nothing to do here.
              </p>
            )}
          </>
        ) : jobRunning ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <div className="relative mb-5 flex size-16 items-center justify-center">
              <span className="absolute inset-0 rounded-full bg-ink/5 animate-pulse-ring" />
              <Loader2 className="size-8 animate-spin text-ink-3" />
            </div>
            <p className="text-sm font-medium text-ink">
              Running TASK MATRIX…
            </p>
            <p className="mt-1.5 max-w-sm text-[13px] text-ink-3">
              Scoring candidate tasks against the rubric. This continues if
              you leave the page.
            </p>
          </div>
        ) : jobError ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-danger/15 text-danger">
              <XCircle className="size-6" />
            </div>
            <p className="text-sm font-medium text-ink">TASK MATRIX failed</p>
            <ErrorBox className="mt-3 max-w-md text-left">{jobError}</ErrorBox>
            <Button
              variant="primary"
              className="mt-5"
              onClick={() => void runMatrix()}
            >
              <RotateCcw className="size-4" />
              Retry
            </Button>
          </div>
        ) : manual ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-surface-2 text-ink-3">
              <FlaskConical className="size-6" />
            </div>
            <p className="max-w-md text-sm text-ink-2">
              Generate candidate tasks for this source, then select one to
              advance or drop the run.
            </p>
            <Button
              variant="primary"
              className="mt-5"
              onClick={() => void runMatrix()}
            >
              <Play className="size-4" />
              Run TASK MATRIX
            </Button>
            {error && <ErrorBox className="mt-4 w-full">{error}</ErrorBox>}
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-surface-2 text-ink-3">
              <FlaskConical className="size-6" />
            </div>
            <p className="max-w-md text-sm text-ink-2">
              Auto-selecting the best candidate…
            </p>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function ErrorBox({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border border-danger/30 bg-danger-soft/20 px-4 py-2.5 text-sm text-danger",
        className,
      )}
    >
      {children}
    </div>
  );
}

function CandidateCard({
  candidate: c,
  index,
  selected,
  onSelect,
}: {
  candidate: TaskCandidate;
  index: number;
  selected: boolean;
  onSelect: () => void;
}) {
  // ProgramBench schema (ADR-0038) headlines tool + upstream language; the legacy rewrite-port
  // fields fill in for old task_matrix.json files (either set may be present, never both).
  const isProgramBench = !!c.tool_name;
  const headline = c.tool_name ?? c.target_language ?? "candidate";
  const subline = isProgramBench
    ? (c.upstream_language ?? "").toUpperCase() || null
    : c.scope_unit
      ? titleCase(c.scope_unit)
      : null;
  const detail = c.flag_surface ?? c.scope_detail;

  return (
    <motion.button
      type="button"
      onClick={onSelect}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.04 }}
      className={cn(
        "focus-ring group relative w-full rounded-2xl border p-4 text-left transition-all",
        selected
          ? "border-accent/60 bg-accent-soft/15 ring-1 ring-accent/40"
          : "border-line bg-bg-2/40 hover:border-line-2 hover:bg-surface-2/50",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "flex size-6 items-center justify-center rounded-full text-[12px] font-semibold",
              selected
                ? "bg-accent text-accent-fg"
                : "bg-surface-2 text-ink-3",
            )}
          >
            {selected ? <Check className="size-3.5" /> : index + 1}
          </span>
          <span className="text-[15px] font-semibold text-ink">{headline}</span>
          {c.binary_name && c.binary_name !== c.tool_name && (
            <span className="font-mono text-[12px] text-ink-4">({c.binary_name})</span>
          )}
          {subline && (
            <>
              <span className="text-ink-4">·</span>
              <span className="text-sm text-ink-2">{subline}</span>
            </>
          )}
        </div>
        <Badge tone={REC_TONE[c.recommendation] ?? "neutral"}>
          {c.recommendation}
        </Badge>
      </div>

      {detail && <p className="mt-2 text-[13px] text-ink-2">{detail}</p>}
      <p className="mt-1.5 text-[13px] leading-relaxed text-ink-3">
        {c.rationale}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12px]">
        {isProgramBench ? (
          <>
            {c.case_families && c.case_families.length > 0 && (
              <Meta icon={Target} label="Families" value={String(c.case_families.length)} />
            )}
            {c.est_kloc != null && (
              <Meta icon={Gauge} label="Size" value={`${c.est_kloc} kLOC`} />
            )}
            {c.expert_hours != null && (
              <Meta icon={Gauge} label="Expert" value={`${c.expert_hours}h`} />
            )}
            {c.needs_files_dir && <Badge tone="info">files_dir</Badge>}
            {c.deterministic_output === false && (
              <Badge tone="danger">non-deterministic output</Badge>
            )}
          </>
        ) : (
          <>
            {c.verifier_mechanism && (
              <Meta icon={Target} label="Verifier" value={titleCase(c.verifier_mechanism)} />
            )}
            {c.objective && (
              <Meta icon={Gauge} label="Objective" value={c.objective.replace(/\+/g, " + ")} />
            )}
          </>
        )}
        <span className="flex items-center gap-1.5 text-ink-3">
          <span className="text-ink-4">Difficulty</span>
          <span
            className={cn(
              "font-medium capitalize",
              DIFFICULTY_TONE[c.expected_difficulty] ?? "text-ink-2",
            )}
          >
            {c.expected_difficulty}
          </span>
        </span>
        {c.license_ok === false && (
          <Badge tone="danger">copyleft: clean-room required</Badge>
        )}
        <span className="ml-auto flex items-center gap-1.5 text-ink-4">
          <BookMarked className="size-3" />
          <span className="font-mono">{c.basis_ref}</span>
        </span>
      </div>
    </motion.button>
  );
}

function Meta({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Target;
  label: string;
  value: string;
}) {
  return (
    <span className="flex items-center gap-1.5 text-ink-3">
      <Icon className="size-3 text-ink-4" />
      <span className="text-ink-4">{label}</span>
      <span className="font-medium text-ink-2">{value}</span>
    </span>
  );
}
