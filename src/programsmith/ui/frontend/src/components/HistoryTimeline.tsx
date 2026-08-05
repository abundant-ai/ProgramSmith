import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { ArrowRight, ChevronDown, ChevronRight, History } from "lucide-react";
import type { StageEvent } from "../api";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";
import { Badge } from "./ui/Badge";
import { relTime, titleCase } from "../lib/format";
import { stageLabel } from "../lib/pipeline";
import { cn } from "../lib/cn";

const VERDICT_TONE: Record<string, "ok" | "warn" | "danger" | "info" | "neutral"> = {
  pass: "ok",
  selected: "ok",
  proceed: "ok",
  clean: "ok",
  accept: "ok",
  done: "info",
  harden: "warn",
  revise: "warn",
  fail: "danger",
  reject: "danger",
  flag_broken: "danger",
  none_selected: "neutral",
};

/** Pull out quantitative specifics (pass@1, bands, gaps, blockers, ε) so the timeline surfaces the
 *  numbers, not just prose. Best-effort regexes over the verdict reason. */
function metricsOf(reason: string): string[] {
  const out: string[] = [];
  const grab = (re: RegExp, label: (m: RegExpMatchArray) => string) => {
    const m = reason.match(re);
    if (m) out.push(label(m));
  };
  grab(/pass@1\s*=?\s*([0-9.]+)/i, (m) => `pass@1 ${m[1]}`);
  grab(/\bcc=([0-9.]+)/i, (m) => `cc ${m[1]}`);
  grab(/\bcx=([0-9.]+)/i, (m) => `cx ${m[1]}`);
  grab(/gap\s*=?\s*([0-9.]+)/i, (m) => `gap ${m[1]}`);
  grab(/([0-9]+)\s*blocker/i, (m) => `${m[1]} blocker(s)`);
  grab(/\b(SOLVABLE_AS_WRITTEN|SOLVABLE_ONLY_BY_GUESSING|UNSOLVABLE)\b/, (m) => m[1]);
  grab(/([0-9]+)\s*suspicious/i, (m) => `${m[1]} suspicious`);
  return out;
}

function absTime(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
}

export function HistoryTimeline({ history }: { history: StageEvent[] }) {
  const items = useMemo(() => [...history].reverse(), [history]); // newest first
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [allOpen, setAllOpen] = useState(false);
  const isOpen = (i: number) => allOpen || open.has(i);
  const toggle = (i: number) =>
    setOpen((prev) => {
      const n = new Set(prev);
      n.has(i) ? n.delete(i) : n.add(i);
      return n;
    });

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <History className="size-4 text-accent" />
          History
        </CardTitle>
        <div className="flex items-center gap-3">
          <span className="text-[12px] text-ink-4">{history.length} events</span>
          {items.length > 0 && (
            <button
              onClick={() => {
                setAllOpen((v) => !v);
                setOpen(new Set());
              }}
              className="focus-ring rounded-md px-2 py-1 text-[12px] font-medium text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
            >
              {allOpen ? "Collapse all" : "Expand all"}
            </button>
          )}
        </div>
      </CardHeader>
      <CardBody>
        {items.length === 0 ? (
          <p className="py-4 text-center text-[13px] italic text-ink-4">
            No transitions recorded yet.
          </p>
        ) : (
          <ol className="relative max-h-[28rem] space-y-0 overflow-y-auto pr-1">
            {items.map((ev, i) => {
              const expanded = isOpen(i);
              const metrics = metricsOf(ev.reason || "");
              return (
                <motion.li
                  key={`${ev.stage}-${i}`}
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: Math.min(i, 12) * 0.025 }}
                  className="relative flex gap-3.5 pb-5 last:pb-0"
                >
                  {/* rail */}
                  <div className="relative flex flex-col items-center">
                    <span
                      className={cn(
                        "z-10 mt-1 size-2.5 rounded-full ring-4 ring-bg",
                        i === 0 ? "bg-accent" : "bg-line-2",
                      )}
                    />
                    {i < items.length - 1 && (
                      <span className="absolute top-3.5 h-full w-px bg-line" />
                    )}
                  </div>

                  <div className="min-w-0 flex-1">
                    <button
                      onClick={() => toggle(i)}
                      className="focus-ring flex w-full flex-wrap items-center gap-2 rounded-md text-left"
                    >
                      {expanded ? (
                        <ChevronDown className="size-3.5 shrink-0 text-ink-4" />
                      ) : (
                        <ChevronRight className="size-3.5 shrink-0 text-ink-4" />
                      )}
                      <span className="text-sm font-medium text-ink">
                        {stageLabel(ev.stage)}
                      </span>
                      <Badge tone={VERDICT_TONE[ev.verdict] ?? "neutral"}>
                        {titleCase(ev.verdict)}
                      </Badge>
                      <ArrowRight className="size-3.5 text-ink-4" />
                      <span className="text-[13px] text-ink-2">{stageLabel(ev.next)}</span>
                      {ev.ts && (
                        <span className="ml-auto text-[12px] text-ink-4">{relTime(ev.ts)}</span>
                      )}
                    </button>

                    {/* collapsed: a single quiet reason line — clean. expanded: metric chips + the
                        full reason + absolute time. Keeps the timeline scannable, detail on demand. */}
                    {ev.reason &&
                      (expanded ? (
                        <div className="mt-1.5 space-y-2 pl-5">
                          {metrics.length > 0 && (
                            <div className="flex flex-wrap gap-1.5">
                              {metrics.map((m, k) => (
                                <span
                                  key={k}
                                  className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-ink-2"
                                >
                                  {m}
                                </span>
                              ))}
                            </div>
                          )}
                          <p className="whitespace-pre-wrap break-words rounded-lg bg-bg-2 px-3 py-2 font-mono text-[12px] leading-relaxed text-ink-2">
                            {ev.reason}
                          </p>
                          {ev.ts && <p className="text-[11px] text-ink-4">{absTime(ev.ts)}</p>}
                        </div>
                      ) : (
                        <p className="mt-1 line-clamp-1 pl-5 text-[13px] text-ink-3">
                          {ev.reason}
                        </p>
                      ))}
                  </div>
                </motion.li>
              );
            })}
          </ol>
        )}
      </CardBody>
    </Card>
  );
}
