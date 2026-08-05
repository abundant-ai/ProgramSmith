import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Gauge, Terminal } from "lucide-react";
import { api } from "../api";
import { usePolling } from "../lib/usePolling";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";
import { cn } from "../lib/cn";

/**
 * Live terminal of the current cell agent (`claude -p`). The session streams JSON events
 * (--output-format stream-json) to <run>/agent-logs/agent.log; we parse them into a readable
 * transcript — the agent's text, thinking, tool calls + results, and final result — interleaved
 * with the pipeline's own deterministic markers ({"type":"lh"}: iteration / validation pass-fail).
 * Polls /agent-output (2s live, 8s idle) and auto-scrolls to the newest line.
 */

type Entry =
  | { kind: "text"; text: string }
  | { kind: "thinking"; text: string }
  | { kind: "tool"; name: string; summary: string }
  | { kind: "tool_result"; text: string; isError: boolean }
  | { kind: "final"; text: string; meta: string; isError: boolean }
  | { kind: "system"; text: string }
  | { kind: "lh"; event: string; ok?: boolean; detail?: string; n?: number; of?: number; label?: string }
  | { kind: "divider"; text: string }
  | { kind: "raw"; text: string };

function toolSummary(name: string, input: unknown): string {
  const o = (input ?? {}) as Record<string, unknown>;
  if (name === "Bash" && typeof o.command === "string") return o.command.split("\n")[0];
  for (const k of ["file_path", "path", "pattern", "url", "query", "command"]) {
    if (typeof o[k] === "string") return o[k] as string;
  }
  const s = JSON.stringify(o);
  return s === "{}" ? "" : s.length > 160 ? s.slice(0, 160) + "…" : s;
}

function blockText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map((c) => (c && typeof c === "object" && "text" in c ? String((c as { text?: unknown }).text ?? "") : ""))
      .join("");
  return "";
}

function parseLog(tail: string): Entry[] {
  const out: Entry[] = [];
  for (const raw of tail.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("=====")) {
      out.push({ kind: "divider", text: line.replace(/=+/g, "").trim() });
      continue;
    }
    let obj: { type?: string; [k: string]: unknown };
    try {
      obj = JSON.parse(line);
    } catch {
      out.push({ kind: "raw", text: line });
      continue;
    }
    if (!obj || typeof obj !== "object") continue;
    const msg = obj.message as { content?: Array<Record<string, unknown>> } | undefined;
    switch (obj.type) {
      case "lh":
        out.push({
          kind: "lh",
          event: String(obj.event ?? ""),
          ok: obj.ok as boolean | undefined,
          detail: obj.detail as string | undefined,
          n: obj.n as number | undefined,
          of: obj.of as number | undefined,
          label: obj.label as string | undefined,
        });
        break;
      case "system":
        if (obj.subtype === "init")
          out.push({ kind: "system", text: `session started${obj.model ? ` · ${obj.model}` : ""}` });
        break;
      case "assistant":
        for (const b of msg?.content ?? []) {
          if (b.type === "text" && typeof b.text === "string" && b.text.trim())
            out.push({ kind: "text", text: b.text.trim() });
          else if (b.type === "thinking" && typeof b.thinking === "string" && b.thinking.trim())
            out.push({ kind: "thinking", text: b.thinking.trim() });
          else if (b.type === "tool_use")
            out.push({ kind: "tool", name: String(b.name ?? "tool"), summary: toolSummary(String(b.name), b.input) });
        }
        break;
      case "user":
        for (const b of msg?.content ?? []) {
          if (b.type === "tool_result") {
            const t = blockText(b.content).trim();
            if (t) out.push({ kind: "tool_result", text: t, isError: !!b.is_error });
          }
        }
        break;
      case "result": {
        const meta = [
          obj.duration_ms ? `${Math.round(Number(obj.duration_ms) / 1000)}s` : null,
          obj.num_turns ? `${obj.num_turns} turns` : null,
          obj.total_cost_usd != null ? `$${Number(obj.total_cost_usd).toFixed(2)}` : null,
        ]
          .filter(Boolean)
          .join(" · ");
        out.push({
          kind: "final",
          text: String(obj.result ?? (obj.is_error ? "errored" : "done")),
          meta,
          isError: !!obj.is_error,
        });
        break;
      }
    }
  }
  return out;
}

const MAX_RESULT_LINES = 8;
function clamp(text: string, n: number): { text: string; clamped: boolean } {
  const lines = text.split("\n");
  return lines.length <= n ? { text, clamped: false } : { text: lines.slice(0, n).join("\n"), clamped: true };
}

function EntryRow({ e }: { e: Entry }) {
  switch (e.kind) {
    case "divider":
      return (
        <div className="my-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-ink-3">
          <span className="h-px flex-1 bg-line-2" />
          {e.text || "session"}
          <span className="h-px flex-1 bg-line-2" />
        </div>
      );
    case "system":
      return <div className="text-[12.5px] text-ink-3">● {e.text}</div>;
    case "lh": {
      const isIter = e.event === "iteration";
      const icon = isIter ? "▶" : e.ok ? "✓" : "✗";
      const color = isIter ? "text-accent" : e.ok ? "text-ok" : "text-warn";
      const head = isIter
        ? `iteration ${e.n}${e.of ? `/${e.of}` : ""}${e.label ? ` · ${e.label}` : ""}`
        : e.event === "validated"
          ? e.ok
            ? `validated · ${e.detail ?? "oracle=1/nop=0"}`
            : `iteration ${e.n} didn't validate · ${e.detail ?? ""}`
          : (e.detail ?? e.event);
      return (
        <div className={cn("flex gap-2 text-[12.5px] font-medium", color)}>
          <span className="shrink-0">{icon}</span>
          <span className="whitespace-pre-wrap break-words">{head}</span>
        </div>
      );
    }
    case "thinking":
      return (
        <div className="flex gap-2 text-[12.5px] italic text-ink-3">
          <span className="shrink-0 not-italic">💭</span>
          <span className="whitespace-pre-wrap break-words">{e.text}</span>
        </div>
      );
    case "tool":
      return (
        <div className="flex gap-2 font-mono text-[12.5px] text-ink-2">
          <span className="shrink-0 text-ink-3">$</span>
          <span className="whitespace-pre-wrap break-words">
            <span className="font-semibold text-info">{e.name}</span>
            {e.summary ? ` ${e.summary}` : ""}
          </span>
        </div>
      );
    case "tool_result": {
      const { text, clamped } = clamp(e.text, MAX_RESULT_LINES);
      return (
        <div className={cn("flex gap-2 font-mono text-[12.5px]", e.isError ? "text-danger" : "text-ink-3")}>
          <span className="shrink-0">↳</span>
          <span className="whitespace-pre-wrap break-words">
            {text}
            {clamped && <span className="text-ink-4"> …</span>}
          </span>
        </div>
      );
    }
    case "final":
      return (
        <div className={cn("flex gap-2 text-[12.5px] font-medium", e.isError ? "text-danger" : "text-ok")}>
          <span className="shrink-0">{e.isError ? "✗" : "✓"}</span>
          <span className="whitespace-pre-wrap break-words">
            {e.text}
            {e.meta && <span className="ml-1 font-normal text-ink-4">({e.meta})</span>}
          </span>
        </div>
      );
    case "text":
      return <div className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-ink">{e.text}</div>;
    case "raw":
      return <div className="whitespace-pre-wrap break-words font-mono text-[12px] text-ink-3">{e.text}</div>;
  }
}

export function AgentOutput({ runKey }: { runKey: string }) {
  const [open, setOpen] = useState(true);
  // poll fast while an agent is live, slow once idle (the last transcript stays visible)
  const [pollMs, setPollMs] = useState(2000);
  const { data } = usePolling(() => api.agentOutput(runKey), pollMs, [runKey, pollMs]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (data) setPollMs(data.running ? 2000 : 8000);
  }, [data?.running]);

  const entries = useMemo(() => (data?.tail ? parseLog(data.tail) : []), [data?.tail]);

  useEffect(() => {
    if (open && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [entries.length, open]);

  if (!data || (!data.exists && !data.running)) return null; // nothing to show yet

  return (
    <Card>
      <CardHeader className="cursor-pointer select-none" onClick={() => setOpen((o) => !o)}>
        <CardTitle>
          <Terminal className="size-4 text-accent" />
          Agent output
          {data.running && (
            <span
              className={cn(
                "ml-1 inline-flex items-center gap-1.5 normal-case tracking-normal text-[12px] font-medium",
                data.slow ? "text-warn" : "text-accent",
              )}
            >
              <span className={cn("inline-block size-1.5 animate-pulse rounded-full", data.slow ? "bg-warn" : "bg-accent")} />
              {data.active_job ?? "running"}
              {data.elapsed_sec != null && ` · ${fmtElapsed(data.elapsed_sec)}`}
            </span>
          )}
        </CardTitle>
        {open ? <ChevronDown className="size-4 text-ink-4" /> : <ChevronRight className="size-4 text-ink-4" />}
      </CardHeader>
      {open && (
        <CardBody className="space-y-3">
          {data.slow && (
            <div className="flex items-start gap-2 rounded-lg border border-warn/30 bg-warn/5 px-3 py-2 text-[12.5px] text-warn">
              <Gauge className="mt-0.5 size-3.5 shrink-0" />
              <span>
                Running slowly ({fmtElapsed(data.elapsed_sec ?? 0)}) — the provider may be{" "}
                <span className="font-medium">rate-limiting</span> this agent.
              </span>
            </div>
          )}
          {entries.length > 0 ? (
            <div ref={scrollRef} className="max-h-[460px] space-y-2 overflow-auto rounded-lg border border-line/70 bg-bg-2 p-4">
              {entries.map((e, i) => (
                <EntryRow key={i} e={e} />
              ))}
              {data.running && (
                <div className="flex items-center gap-1.5 pt-1 text-[12px] text-ink-4">
                  <span className="inline-block size-1.5 animate-pulse rounded-full bg-accent" />
                  working…
                </div>
              )}
            </div>
          ) : (
            <p className="text-[13px] text-ink-4">
              {data.running ? "Agent starting…" : "No agent output captured yet."}
            </p>
          )}
        </CardBody>
      )}
    </Card>
  );
}

function fmtElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}
