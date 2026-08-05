import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Feather,
  FileText,
  FolderOpen,
  Pause,
  Play,
  RotateCcw,
  ShieldAlert,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api";
import { usePolling } from "../lib/usePolling";
import { Card, CardBody, CardHeader, CardTitle } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { Tooltip } from "../components/ui/Tooltip";
import { Skeleton } from "../components/ui/Skeleton";
import { ErrorState } from "../components/ui/States";
import { PipelineDag } from "../components/PipelineDag";
import { RunContextPanel } from "../components/RunContextPanel";
import { HistoryTimeline } from "../components/HistoryTimeline";
import { FileExplorer } from "../components/FileExplorer";
import { AgentOutput } from "../components/AgentOutput";
import { TerminalPanel } from "../components/TerminalPanel";
import { TaskMatrixReview } from "../components/TaskMatrixReview";
import { QaGatePanel } from "../components/QaGatePanel";
import { GitBranch, Workflow } from "lucide-react";
import { shortSha } from "../lib/format";
import { FORWARD_STAGES, SYNTHESIZE_META } from "../lib/pipeline";

export function RunPage() {
  const { key = "" } = useParams();
  const navigate = useNavigate();
  const [filesOpen, setFilesOpen] = useState(false);
  const [promptPath, setPromptPath] = useState<string | null>(null);
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const { data, error, initialLoading, refresh } = usePolling(
    () => api.getRun(key),
    3000,
    [key],
  );

  const togglePause = useCallback(async () => {
    if (!data) return;
    if (data.summary.paused) await api.resume(key);
    else await api.pause(key);
    await refresh();
  }, [data, key, refresh]);

  const [retrying, setRetrying] = useState(false);
  const doRetry = useCallback(async () => {
    setRetrying(true);
    try {
      await api.retry(key); // clear errored job(s) → driver re-runs the blocked stage fresh
      await refresh();
    } finally {
      setRetrying(false);
    }
  }, [key, refresh]);

  const [deleting, setDeleting] = useState(false);
  const doDelete = useCallback(async () => {
    if (
      !window.confirm(
        `Delete run "${key}" permanently? This removes its state and task files and cannot be undone.`,
      )
    )
      return;
    setDeleting(true);
    try {
      await api.deleteRun(key);
      navigate("/"); // run is gone → back to the fleet
    } catch {
      setDeleting(false); // stay on the page if it failed
    }
  }, [key, navigate]);

  if (initialLoading) return <RunSkeleton />;

  if (error && !data) {
    return (
      <div className="space-y-5">
        <BackLink />
        <ErrorState
          title="Run not found"
          message={error.message}
          action={
            <Link to="/">
              <Button variant="secondary">Back to fleet</Button>
            </Link>
          }
        />
      </div>
    );
  }

  if (!data) return <RunSkeleton />;

  const { summary, node_statuses, history, context } = data;
  const src = context.source;

  return (
    <div className="space-y-6">
      <BackLink />

      {/* header */}
      <Card>
        <CardBody className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <h1 className="text-2xl font-semibold tracking-tight text-ink">
                {summary.slug ?? summary.key}
              </h1>
              <StatusBadge status={summary.status} stage={summary.stage} screenedOut={!!summary.screened_out} />
              {summary.paused && (
                <Badge tone="warn">
                  <Pause className="size-3" />
                  Paused
                </Badge>
              )}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px] text-ink-3">
              <span className="flex items-center gap-1.5 font-mono">
                <GitBranch className="size-3.5 text-ink-4" />
                {summary.key}
              </span>
              {src && (
                <span className="font-mono text-ink-4">
                  {src.repo}@{shortSha(src.pinned_sha)}
                </span>
              )}
              {summary.harden > 0 && (
                <span className="flex items-center gap-1.5 text-warn">
                  <ShieldAlert className="size-3.5" />
                  harden {summary.harden}
                </span>
              )}
              {(summary.ease ?? 0) > 0 && (
                <span className="flex items-center gap-1.5 text-info">
                  <Feather className="size-3.5" />
                  ease {summary.ease}
                </span>
              )}
              {summary.revise > 0 && (
                <span className="flex items-center gap-1.5 text-info">
                  <RotateCcw className="size-3.5" />
                  revise {summary.revise}
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            <Tooltip content="Browse the run's task files, source, and artifacts" side="bottom">
              <Button variant="outline" size="sm" onClick={() => setFilesOpen(true)}>
                <FolderOpen className="size-3.5" />
                Files
              </Button>
            </Tooltip>
            {data.drive?.halted === "blocked" && (
              <Tooltip
                content="Retry — clear the errored job(s) so the driver re-runs this blocked stage fresh (use after fixing the cause)"
                side="bottom"
              >
                <Button
                  variant="secondary"
                  onClick={() => void doRetry()}
                  disabled={retrying}
                >
                  <RotateCcw className="size-4" />
                  Retry
                </Button>
              </Tooltip>
            )}
            {summary.status !== "draft" && (
              <Tooltip
                content={
                  summary.paused
                    ? "Resume — allow the run to advance"
                    : "Pause — halt at the next inter-stage checkpoint"
                }
                side="bottom"
              >
                <Button
                  variant={summary.paused ? "primary" : "secondary"}
                  onClick={() => void togglePause()}
                >
                  {summary.paused ? (
                    <>
                      <Play className="size-4" />
                      Resume
                    </>
                  ) : (
                    <>
                      <Pause className="size-4" />
                      Pause
                    </>
                  )}
                </Button>
              </Tooltip>
            )}
          </div>
        </CardBody>
      </Card>

      {/* terminal (dropped/blocked/easy): rich panel with the WHY + harden-review + re-open.
          A DONE run needs no panel — the export is conveyed by the green "Done" DAG node. */}
      {(data.waiting?.kind === "terminal" || data.waiting?.kind === "draft") &&
        summary.status !== "done" && (
        <TerminalPanel
          runKey={key}
          status={summary.status}
          reason={data.waiting.reason}
          canReopen={!!data.waiting.can_reopen}
          hardenHistory={context.harden_history ?? []}
          screenedOut={!!summary.screened_out}
          onReopened={() => void refresh()}
        />
      )}

      {/* DAG */}
      <Card>
        <CardHeader>
          <CardTitle>
            <Workflow className="size-4 text-accent" />
            Pipeline
          </CardTitle>
        </CardHeader>
        <CardBody>
          <PipelineDag
            statuses={node_statuses}
            selected={selectedStage}
            onSelectStage={(s) => setSelectedStage((cur) => (cur === s ? null : s))}
          />
          {selectedStage && (
            <StepInspector
              stage={selectedStage}
              status={node_statuses[selectedStage]}
              history={history}
              onClose={() => setSelectedStage(null)}
              onViewPrompt={(p) => {
                setPromptPath(p);
                setFilesOpen(true);
              }}
            />
          )}
        </CardBody>
      </Card>

      {/* human reviews (conditional) */}
      {summary.status === "in_progress" && summary.stage === "TASK_MATRIX" && (
        <TaskMatrixReview
          runKey={key}
          jobs={data.jobs}
          manual={summary.awaiting_human}
          onAdvanced={() => void refresh()}
        />
      )}
      {summary.status === "in_progress" && summary.stage === "QA_GATE" && (
        <QaGatePanel runKey={key} context={context} onDecided={() => void refresh()} />
      )}

      {/* context + history — items-start so each card wraps its own content (History's scroll
          container fills its card instead of leaving a gap below a stretched card) */}
      <div className="grid items-start gap-6 lg:grid-cols-2">
        <RunContextPanel context={context} />
        <HistoryTimeline history={history} />
      </div>

      {/* live cell-agent terminal — under history (auto-tails while a claude -p worker runs) */}
      <AgentOutput runKey={key} />

      <FileExplorer
        runKey={key}
        open={filesOpen}
        initialPath={promptPath}
        onClose={() => {
          setFilesOpen(false);
          setPromptPath(null);
        }}
      />

      {/* delete — kept at the very bottom, out of the way of the run's live state */}
      <div className="flex justify-end border-t border-line/60 pt-5">
        <Button
          variant="ghost"
          className="text-ink-4 hover:text-danger"
          onClick={() => void doDelete()}
          disabled={deleting}
        >
          <Trash2 className="size-4" />
          Delete run
        </Button>
      </div>
    </div>
  );
}

/** Stages whose LLM cell persists its exact prompt to the run tree (see backend promptlog.py). */
const STAGE_PROMPTS: Record<string, string> = {
  TASK_MATRIX: "prompts/TASK_MATRIX.md",
  SYNTHESIZE: "prompts/SYNTHESIZE.md",
};

function stageMeta(stage: string): { label: string; blurb?: string } {
  const f = FORWARD_STAGES.find((s) => s.stage === stage);
  if (f) return { label: f.label, blurb: f.blurb };
  if (stage === "SYNTHESIZE")
    return { label: SYNTHESIZE_META.label, blurb: SYNTHESIZE_META.blurb };
  return { label: stage };
}

/** Inline step inspector: click a pipeline node → its info + recorded history, plus a link to the
 *  exact prompt the LLM cell used (opened in the side file viewer). */
function StepInspector({
  stage,
  status,
  history,
  onClose,
  onViewPrompt,
}: {
  stage: string;
  status?: string;
  history: Array<{ stage: string; verdict?: string | null; reason?: string | null }>;
  onClose: () => void;
  onViewPrompt: (path: string) => void;
}) {
  const meta = stageMeta(stage);
  const events = history.filter((e) => e.stage === stage);
  const prompt = STAGE_PROMPTS[stage];
  return (
    <div className="mt-4 rounded-xl border border-accent/40 bg-surface-2 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-semibold text-ink">{meta.label}</span>
            <Badge tone="neutral">{status ?? "pending"}</Badge>
          </div>
          {meta.blurb && (
            <p className="mt-1 text-[12.5px] leading-snug text-ink-3">{meta.blurb}</p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {prompt && (
            <Button size="sm" variant="outline" onClick={() => onViewPrompt(prompt)}>
              <FileText className="size-3.5" />
              View prompt
            </Button>
          )}
          <button
            onClick={onClose}
            className="rounded-md p-1 text-ink-4 transition-colors hover:text-ink"
            title="Close"
          >
            <X className="size-4" />
          </button>
        </div>
      </div>
      <div className="mt-3 border-t border-line pt-3">
        {events.length > 0 ? (
          <ul className="space-y-1.5">
            {events.slice(-6).map((e, i) => (
              <li key={i} className="flex items-start gap-2 text-[12.5px]">
                <span className="shrink-0 rounded bg-bg-2 px-1.5 py-0.5 font-mono text-[11px] text-ink-3">
                  {e.verdict ?? "—"}
                </span>
                <span className="text-ink-2">{e.reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12.5px] text-ink-4">
            No recorded events for this stage yet
            {prompt ? " — the prompt appears here once the cell runs." : "."}
          </p>
        )}
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/"
      className="inline-flex items-center gap-1.5 text-[13px] font-medium text-ink-3 transition-colors hover:text-ink"
    >
      <ArrowLeft className="size-4" />
      Runs
    </Link>
  );
}

function RunSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-5 w-16" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-[360px] w-full" />
      <div className="grid gap-6 lg:grid-cols-2">
        <Skeleton className="h-80 w-full" />
        <Skeleton className="h-80 w-full" />
      </div>
    </div>
  );
}
