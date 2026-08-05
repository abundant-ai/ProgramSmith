import { useState } from "react";
import { Archive, CircleSlash, Lock, PackageCheck, RotateCw } from "lucide-react";
import { api, type HardenGeneration, type RunStatus } from "../api";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";
import { Button } from "./ui/Button";

/** Per-terminal presentation: EASY_SHELF ("easy") and DONE ("done") are successes with different
 *  colors, dropped/blocked are the warn treatments. Anything unknown falls back to blocked. */
const TERMINAL_META: Record<
  string,
  { Icon: typeof Lock; title: string; tone: "ok" | "warn" }
> = {
  draft: { Icon: PackageCheck, title: "Draft exported after Static CI", tone: "ok" },
  dropped: { Icon: CircleSlash, title: "Run dropped", tone: "warn" },
  blocked: { Icon: Lock, title: "Run blocked", tone: "warn" },
  done: { Icon: PackageCheck, title: "Exported to outbox", tone: "ok" },
  easy: { Icon: Archive, title: "Easy shelf", tone: "warn" },
};

/**
 * Shown when a run ended terminally (dropped / blocked / exported / easy-shelved). Explains WHY —
 * the specific reason the driver recorded (e.g. the harden-review auditor's verdict) plus the
 * per-generation hardening review — and, when the outcome is overridable (dropped and easy-shelf
 * runs), lets the operator re-open it for another harden attempt.
 */
export function TerminalPanel({
  runKey,
  status,
  reason,
  canReopen,
  hardenHistory,
  onReopened,
  screenedOut = false,
}: {
  runKey: string;
  status: RunStatus;
  reason: string;
  canReopen: boolean;
  hardenHistory: HardenGeneration[];
  onReopened: () => Promise<void> | void;
  screenedOut?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const base = TERMINAL_META[status] ?? TERMINAL_META.blocked;
  const { Icon, title, tone } = screenedOut
    ? { Icon: CircleSlash, title: "Source screened out", tone: "warn" as const }
    : base;

  const reopen = async () => {
    setBusy(true);
    try {
      await api.reopen(runKey);
      await onReopened();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className={tone === "ok" ? "border-ok/30" : "border-warn/30"}>
      <CardHeader>
        <CardTitle className={tone === "ok" ? "text-ok" : "text-warn"}>
          <Icon className="size-4" />
          {title}
        </CardTitle>
        {canReopen && (
          <Button variant="secondary" size="sm" onClick={() => void reopen()} disabled={busy}>
            <RotateCw className={`size-3.5 ${busy ? "animate-spin" : ""}`} />
            {busy ? "Re-opening…" : "Re-open & harden"}
          </Button>
        )}
      </CardHeader>
      <CardBody className="space-y-4">
        <p className="text-[13.5px] leading-relaxed text-ink-2">{reason}</p>

        {hardenHistory.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-ink-4">
              Hardening review
            </h4>
            <div className="overflow-hidden rounded-lg border border-line">
              {hardenHistory.map((g, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between gap-3 border-b border-line px-3 py-2 text-[12.5px] last:border-b-0"
                >
                  <span className="text-ink-3">
                    {g.stage ?? "harden"} · gen {g.generation ?? i}
                  </span>
                  <span className="flex items-center gap-3 font-mono">
                    <span className="text-ink-2">
                      pass@1 {g.pass_at_1 == null ? "—" : `${Math.round(g.pass_at_1 * 100)}%`}
                    </span>
                    <span className={g.verdict === "drop" ? "text-warn" : "text-ink-3"}>
                      {g.verdict ?? "—"}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {canReopen && (
          <p className="text-[12px] text-ink-4">
            Re-opening grants a fresh tuning budget. If the task is fundamentally too easy (the model
            still solves it), it will honestly land back on the easy shelf — that's a scope problem,
            not a harden one.
          </p>
        )}
      </CardBody>
    </Card>
  );
}
