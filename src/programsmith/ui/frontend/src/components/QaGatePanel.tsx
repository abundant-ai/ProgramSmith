import { useEffect, useState } from "react";
import { CheckCircle2, Cpu, RotateCcw, ShieldCheck, XCircle } from "lucide-react";
import { api, ApiError, type RunContext } from "../api";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";
import { Badge } from "./ui/Badge";
import { cn } from "../lib/cn";

type Decision = "accept" | "revise" | "reject";

/*
 * The FINAL GATE. Mode-aware (ADR-0039):
 *  - qa_gate_mode === "human": accept/revise/reject buttons → POST /api/runs/{key}/qa-gate,
 *    which advances the FSM (accept → DONE + outbox export, revise → SYNTHESIZE, reject →
 *    DROPPED) and logs the backward move to WORKFLOW_NOTES.md.
 *  - qa_gate_mode === "auto" (default): the driver computes the verdict deterministically from
 *    the recorded sweep evidence — no buttons, just a passive note (POSTing would 409 anyway).
 * The mode comes from GET /api/settings; a backend without the key predates ADR-0039 and only
 * ever had the human behavior, so absent ⇒ "human".
 */
export function QaGatePanel({
  runKey,
  context,
  onDecided,
}: {
  runKey: string;
  context: RunContext;
  onDecided?: () => void;
}) {
  const sweeps = (context.sweeps ?? {}) as Record<string, any>;
  const full = sweeps.full ?? sweeps.full_sweep ?? null;

  const [mode, setMode] = useState<"auto" | "human" | null>(null); // null = still resolving
  const [pending, setPending] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api
      .getSettings()
      .then((s) => active && setMode(s.qa_gate_mode === "auto" ? "auto" : "human"))
      .catch(() => active && setMode("human")); // unreachable settings ⇒ old backend ⇒ human
    return () => {
      active = false;
    };
  }, []);

  const decide = async (decision: Decision) => {
    setPending(decision);
    setError(null);
    try {
      const r = await api.qaGate(runKey, decision);
      setDone(`${decision} → ${r.stage} (${r.status})`);
      onDecided?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : `${decision} failed`);
    } finally {
      setPending(null);
    }
  };

  // Frontier evidence — the new generic record {pass_at_1, band_verdict, hard_keep}; the legacy
  // dual-family fields (claude_code) fill in for pre-pivot runs.
  const passAt1 = full?.pass_at_1 ?? full?.claude_code ?? null;
  const stats = [
    { label: "Band verdict", value: String(full?.band_verdict ?? "pending") },
    {
      label: "Frontier pass@1",
      value:
        typeof passAt1 === "number" ? `${Math.round(passAt1 * 100)}%` : String(passAt1 ?? "—"),
    },
    {
      label: "Hard keep",
      value: full?.hard_keep ? "yes — capability headroom" : "—",
    },
  ];

  const human = mode === "human";

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <ShieldCheck className="size-4 text-node-decision" />
          Final Gate
        </CardTitle>
        {mode === "auto" ? (
          <Badge tone="info">
            <Cpu className="size-3" />
            Auto gate
          </Badge>
        ) : (
          <Badge tone="human">Final accept</Badge>
        )}
      </CardHeader>
      <CardBody className="space-y-5">
        <p className="text-[13px] leading-relaxed text-ink-2">
          {human
            ? "Review the frontier evidence before deciding. Accept exports the task bundle to the outbox; revise re-runs the frontier sweep through SYNTHESIZE; reject drops the run."
            : "This gate decides automatically from the recorded evidence (band verdict, integrity, probe, analysis labels) — no action needed. Set qa_gate_mode to “human” in Settings to review manually."}
        </p>

        <div className="grid gap-3 sm:grid-cols-3">
          {stats.map((s) => (
            <BandStat key={s.label} label={s.label} value={s.value} />
          ))}
        </div>

        {human && (
          <div className="flex flex-wrap items-center gap-2.5 border-t border-line pt-4">
            <GateButton
              icon={CheckCircle2}
              label="Accept"
              tone="ok"
              loading={pending === "accept"}
              disabled={pending !== null}
              onClick={() => void decide("accept")}
            />
            <GateButton
              icon={RotateCcw}
              label="Revise"
              tone="info"
              loading={pending === "revise"}
              disabled={pending !== null}
              onClick={() => void decide("revise")}
            />
            <GateButton
              icon={XCircle}
              label="Reject"
              tone="danger"
              loading={pending === "reject"}
              disabled={pending !== null}
              onClick={() => void decide("reject")}
            />
            {done && (
              <span className="ml-auto text-[12px] font-medium text-ok">{done}</span>
            )}
            {error && (
              <span className="ml-auto text-[12px] font-medium text-danger">{error}</span>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function BandStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-line bg-bg-2/40 px-3.5 py-3">
      <div className="text-[11px] uppercase tracking-[0.06em] text-ink-4">
        {label}
      </div>
      <div className="mt-1 truncate font-mono text-sm text-ink-2">{value}</div>
    </div>
  );
}

function GateButton({
  icon: Icon,
  label,
  tone,
  loading,
  disabled,
  onClick,
}: {
  icon: typeof CheckCircle2;
  label: string;
  tone: "ok" | "info" | "danger";
  loading: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const styles = {
    ok: "border-ok/30 text-ok hover:bg-ok-soft/20",
    info: "border-info/30 text-info hover:bg-info/10",
    danger: "border-danger/30 text-danger hover:bg-danger-soft/20",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-10 items-center gap-2 rounded-xl border bg-transparent px-4 text-sm font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        styles,
      )}
    >
      <Icon className={cn("size-4", loading && "animate-pulse")} />
      {loading ? "Submitting…" : label}
    </button>
  );
}
