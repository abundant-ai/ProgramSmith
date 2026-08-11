import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  Cloud,
  Download,
  ExternalLink,
  Play,
  Settings,
  TerminalSquare,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api, ApiError, type OddishRun } from "../api";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";

type Artifact = {
  available: boolean;
  download_url: string;
  calibrated: boolean;
};

type TimelineItem = {
  label: string;
  detail?: string;
  raw?: unknown;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function trajectoryItems(payload: unknown): TimelineItem[] {
  const candidates = [
    payload,
    isRecord(payload) ? payload.steps : undefined,
    isRecord(payload) ? payload.events : undefined,
    isRecord(payload) && isRecord(payload.trajectory) ? payload.trajectory.steps : undefined,
    isRecord(payload) && isRecord(payload.trajectory) ? payload.trajectory.events : undefined,
  ];
  const rows = candidates.find(Array.isArray) as unknown[] | undefined;
  if (!rows) return [];
  return rows.slice(-80).map((row, index) => {
    if (!isRecord(row)) return { label: `Step ${index + 1}`, detail: String(row), raw: row };
    const role = text(row.role);
    const source = text(row.source);
    const kind = text(row.type) ?? text(row.kind) ?? text(row.event);
    const tool = text(row.tool_name) ?? text(row.tool);
    const toolCalls = Array.isArray(row.tool_calls) ? row.tool_calls.filter(isRecord) : [];
    const toolNames = toolCalls
      .map((call) => text(call.function_name) ?? text(call.name))
      .filter((name): name is string => !!name);
    const observations = isRecord(row.observation) && Array.isArray(row.observation.results)
      ? row.observation.results.filter(isRecord).map((item) => text(item.content)).filter((value): value is string => !!value)
      : [];
    const label = tool
      ? `Tool · ${tool}`
      : toolNames.length
        ? `Tool · ${toolNames.join(", ")}`
        : source ?? role ?? (kind ? kind.replaceAll("_", " ") : `Step ${index + 1}`);
    const detail =
      text(row.reasoning_content) ??
      text(row.content) ??
      text(row.message) ??
      text(row.text) ??
      text(row.command) ??
      (observations.length ? observations.join("\n") : undefined) ??
      (isRecord(row.function) ? text(row.function.name) : undefined);
    return { label, detail, raw: row };
  });
}

function statusTone(status: string): "neutral" | "ok" | "warn" | "danger" | "info" {
  if (["complete", "completed", "success", "passed"].includes(status)) return "ok";
  if (["failed", "error", "cancelled"].includes(status)) return "danger";
  if (["queued", "submitting", "running", "pending"].includes(status)) return "info";
  return "neutral";
}

export function OddishPanel({
  runKey,
  artifact,
}: {
  runKey: string;
  artifact?: Artifact;
}) {
  const [oddish, setOddish] = useState<OddishRun | null>(null);
  const [trajectory, setTrajectory] = useState<unknown>(null);
  const [launching, setLaunching] = useState(false);
  const [loadingTrajectory, setLoadingTrajectory] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.oddishStatus(runKey);
      setOddish(next);
      setError(next.error ?? next.refresh_error ?? null);
    } catch (caught) {
      if (!(caught instanceof ApiError && caught.status === 404)) {
        setError(caught instanceof Error ? caught.message : "Could not load Oddish status");
      }
    }
  }, [runKey]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!oddish || !["submitting", "queued", "running", "pending"].includes(oddish.status)) return;
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [oddish, refresh]);

  const trial = oddish?.trials?.[0];
  const running = !!oddish && ["submitting", "queued", "running", "pending"].includes(oddish.status);
  const timeline = useMemo(() => trajectoryItems(trajectory), [trajectory]);

  const launch = useCallback(async () => {
    setLaunching(true);
    setError(null);
    try {
      setOddish(await api.runOnOddish(runKey));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start the Oddish run");
    } finally {
      setLaunching(false);
    }
  }, [runKey]);

  const loadTrajectory = useCallback(async () => {
    setLoadingTrajectory(true);
    setError(null);
    try {
      setTrajectory(await api.oddishTrajectory(runKey, trial?.id ?? undefined));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load the trajectory");
    } finally {
      setLoadingTrajectory(false);
    }
  }, [runKey, trial?.id]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Cloud className="size-4 text-accent" />
          Task
        </CardTitle>
        {oddish && oddish.status !== "idle" && (
          <Badge tone={statusTone(oddish.status)} className="capitalize">
            {running && <span className="size-1.5 animate-pulse bg-current" />}
            {oddish.status}
          </Badge>
        )}
      </CardHeader>
      <CardBody className="space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="max-w-xl">
            <h2 className="text-lg font-semibold tracking-tight text-ink">
              {artifact?.available ? "Your task is ready" : "Building your task"}
            </h2>
            <p className="mt-1 text-sm leading-relaxed text-ink-3">
              {artifact?.available
                ? "Download the portable task, or use your Oddish free-plan quota for one agent trial and a public result."
                : "Download and cloud execution become available after Static CI passes."}
            </p>
          </div>
          <div className="flex flex-wrap gap-2.5">
            {artifact?.available && (
              <Button
                component="a"
                href={artifact.download_url}
                variant="outline"
              >
                <Download className="size-4" />
                Download task
              </Button>
            )}
            <Button
              variant="primary"
              onClick={() => void launch()}
              loading={launching}
              disabled={!artifact?.available || running || oddish?.status === "complete"}
            >
              <Play className="size-4" />
              {running ? "Running on Oddish" : oddish?.status === "complete" ? "Run complete" : "Run on Oddish"}
            </Button>
          </div>
        </div>

        {artifact?.available && !artifact.calibrated && (
          <p className="border-l-2 border-warn pl-3 text-xs leading-relaxed text-ink-3">
            This is a draft task: Static CI passed, but model difficulty has not been calibrated.
          </p>
        )}

        {error && (
          <div className="flex flex-wrap items-center justify-between gap-3 border border-danger/40 bg-danger-soft/20 px-4 py-3 text-sm text-danger">
            <span>{error}</span>
            {error.toLowerCase().includes("key") && (
              <Link to="/settings">
                <Button variant="outline" size="sm">
                  <Settings className="size-3.5" />
                  Add Oddish key
                </Button>
              </Link>
            )}
          </div>
        )}

        {oddish && oddish.status !== "idle" && (
          <div className="border-t border-line pt-5">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Metric label="Agent" value={trial?.agent ?? oddish.agent ?? "—"} />
              <Metric label="Model" value={trial?.model ?? oddish.model ?? "—"} />
              <Metric
                label="Reward"
                value={trial?.reward == null ? "—" : trial.reward.toFixed(2)}
              />
              <Metric
                label="Duration"
                value={trial?.duration_seconds == null ? "—" : `${Math.round(trial.duration_seconds)}s`}
              />
            </div>

            <div className="mt-5 flex flex-wrap items-center gap-2.5">
              {trial?.id && (
                <Button variant="outline" size="sm" onClick={() => void loadTrajectory()} loading={loadingTrajectory}>
                  <TerminalSquare className="size-3.5" />
                  {trajectory ? "Refresh trajectory" : "View trajectory"}
                </Button>
              )}
              {oddish.public_url && (
                <Button component="a" href={oddish.public_url} target="_blank" rel="noreferrer" variant="outline" size="sm">
                  <ExternalLink className="size-3.5" />
                  Public experiment
                </Button>
              )}
            </div>
          </div>
        )}

        {trajectory !== null && (
          <div className="border-t border-line pt-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-ink">Agent trajectory</h3>
              <span className="font-mono text-[11px] text-ink-4">{timeline.length} events</span>
            </div>
            {timeline.length ? (
              <ol className="max-h-[440px] divide-y divide-line overflow-y-auto border border-line">
                {timeline.map((item, index) => (
                  <li key={index} className="grid grid-cols-[22px_minmax(0,1fr)] gap-3 px-4 py-3">
                    <span className="mt-0.5 flex size-5 items-center justify-center border border-line font-mono text-[10px] text-ink-4">
                      {index + 1}
                    </span>
                    <div className="min-w-0">
                      <p className="text-xs font-semibold capitalize text-ink">{item.label}</p>
                      {item.detail && (
                        <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-ink-3">
                          {item.detail.length > 900 ? `${item.detail.slice(0, 900)}…` : item.detail}
                        </p>
                      )}
                      {!item.detail && item.raw !== undefined && (
                        <details className="mt-1 text-[11px] text-ink-4">
                          <summary className="flex cursor-pointer items-center gap-1">
                            Raw event <ChevronDown className="size-3" />
                          </summary>
                          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap font-mono">
                            {JSON.stringify(item.raw, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="flex items-center gap-2 border border-line px-4 py-3 text-sm text-ink-3">
                <Check className="size-4 text-ok" />
                The trajectory is available in the public Oddish experiment.
              </div>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-l border-line pl-3">
      <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-4">{label}</p>
      <p className="mt-1 truncate text-sm text-ink" title={value}>{value}</p>
    </div>
  );
}
