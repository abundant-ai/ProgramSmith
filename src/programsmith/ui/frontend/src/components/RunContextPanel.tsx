import type { ReactNode } from "react";
import {
  Activity,
  Boxes,
  FileCode2,
  GitCommitHorizontal,
  Scale,
  TestTube,
} from "lucide-react";
import type { RunContext } from "../api";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/Card";
import { Badge } from "./ui/Badge";
import { compactNum, shortSha, titleCase } from "../lib/format";
import { HARNESS_PROVIDER, ProviderLogo, providerForModel } from "./Logos";
import type { StageSpec } from "../api";

const LICENSE_TONE: Record<string, "ok" | "warn" | "danger" | "neutral"> = {
  permissive: "ok",
  "weak-copyleft": "warn",
  "strong-copyleft": "danger",
  unknown: "neutral",
};

/** Friendly label for a sweep key. The persisted manifest keys stay "difficulty"/"full"
 *  (never renamed — ADR-0040); only the display names read smoke/frontier. */
const SWEEP_LABELS: Record<string, string> = {
  difficulty: "Smoke",
  full: "Frontier",
  calibrate: "Calibrate",
  qa: "QA",
};
function sweepLabel(key: string): string {
  return (
    SWEEP_LABELS[key] ??
    key.charAt(0).toUpperCase() + key.slice(1).replace(/[-_]/g, " ")
  );
}

export function RunContextPanel({ context }: { context: RunContext }) {
  const src = context.source ?? null;
  const dims = context.dimensions ?? null;
  const oracle = (context.oracle ?? null) as Record<string, any> | null;
  const sweeps = (context.sweeps ?? {}) as Record<string, Record<string, any>>;
  const sweepEntries = Object.entries(sweeps).filter(
    ([, v]) => v && typeof v === "object",
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Boxes className="size-4 text-accent" />
          Run Context
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-6">
        {/* source */}
        <Section title="Source" icon={<GitCommitHorizontal className="size-3.5" />}>
          {src ? (
            <div className="space-y-2.5">
              <Row label="Repository">
                <span className="font-mono text-ink">{src.repo}</span>
              </Row>
              <Row label="Pinned SHA">
                <span className="font-mono text-ink-2">
                  {shortSha(src.pinned_sha, 12)}
                </span>
              </Row>
              <Row label="Language">
                <span className="flex items-center gap-1.5 text-ink-2">
                  <FileCode2 className="size-3.5 text-ink-4" />
                  {src.primary_language ?? "—"}
                </span>
              </Row>
              <Row label="License">
                <span className="flex items-center gap-2">
                  <span className="text-ink-2">{src.license ?? "unknown"}</span>
                  <Badge tone={LICENSE_TONE[src.license_class] ?? "neutral"}>
                    <Scale className="size-3" />
                    {src.license_class}
                  </Badge>
                </span>
              </Row>
              {(src.size_loc != null || src.size_files != null) && (
                <Row label="Size">
                  <span className="text-ink-2">
                    {compactNum(src.size_loc)} LOC · {compactNum(src.size_files)}{" "}
                    files
                  </span>
                </Row>
              )}
              {(src.build_systems.length > 0 ||
                src.test_frameworks.length > 0) && (
                <Row label="Toolchain">
                  <span className="flex flex-wrap justify-end gap-1.5">
                    {src.build_systems.map((b) => (
                      <Badge key={b} tone="neutral">
                        {b}
                      </Badge>
                    ))}
                    {src.test_frameworks.map((t) => (
                      <Badge key={t} tone="info">
                        <TestTube className="size-3" />
                        {t}
                      </Badge>
                    ))}
                  </span>
                </Row>
              )}
            </div>
          ) : (
            <Muted>Source not yet ingested.</Muted>
          )}
        </Section>

        {/* dimensions — ProgramBench axes (ADR-0038) when present, legacy rewrite-port axes for old
            manifests. A compact chip row (tool · binary · language) with the flag surface — the
            grader's exercised scope — as a quiet description line, never a giant pill. */}
        {dims &&
          (dims.tool_name ||
            dims.target_language ||
            dims.scope_unit ||
            dims.verifier_mechanism ||
            dims.objective) && (
            <Section title="Dimensions">
              <div className="flex flex-wrap gap-1.5">
                {dims.tool_name && <Badge tone="accent">{dims.tool_name}</Badge>}
                {dims.binary_name && dims.binary_name !== dims.tool_name && (
                  <Badge tone="neutral">bin: {dims.binary_name}</Badge>
                )}
                {dims.upstream_language && <Badge tone="info">{dims.upstream_language}</Badge>}
                {dims.target_language && <Badge tone="accent">{dims.target_language}</Badge>}
                {dims.scope_unit && <Badge tone="neutral">{titleCase(dims.scope_unit)}</Badge>}
                {dims.verifier_mechanism && (
                  <Badge tone="info">{titleCase(dims.verifier_mechanism)}</Badge>
                )}
                {dims.objective && (
                  <Badge tone="neutral">{dims.objective.replace(/\+/g, " + ")}</Badge>
                )}
              </div>
              {dims.flag_surface && (
                <p className="mt-2 text-[12.5px] leading-relaxed text-ink-3">{dims.flag_surface}</p>
              )}
            </Section>
          )}

        {/* oracle — ProgramBench bundle fields (n_cases, byte-distinct pair); hidden until captured.
            epsilon rows only for legacy manifests (ProgramBench grading is byte-exact) */}
        {oracle && (
          <Section title="Oracle">
            <div className="space-y-2.5">
              {oracle.approach && (
                <Row label="Approach">
                  <span className="text-ink-2">
                    {titleCase(String(oracle.approach).replace(/-/g, " "))}
                  </span>
                </Row>
              )}
              {oracle.n_cases != null && (
                <Row label="Golden cases">
                  <span className="font-mono text-ink-2">{String(oracle.n_cases)}</span>
                </Row>
              )}
              {oracle.text_distinct != null && (
                <Row label="Oracle pair">
                  <span className={oracle.text_distinct ? "text-ok" : "text-danger"}>
                    {oracle.text_distinct ? "byte-distinct" : "NOT distinct"}
                  </span>
                </Row>
              )}
              {oracle.epsilon != null &&
                (typeof oracle.epsilon === "object" ? (
                  <div>
                    <div className="mb-1.5 text-[13px] text-ink-3">Epsilon (ε), per field</div>
                    <div className="space-y-1 rounded-lg bg-bg-2 px-3 py-2">
                      {Object.entries(oracle.epsilon as Record<string, unknown>).map(
                        ([field, tol]) => (
                          <div
                            key={field}
                            className="flex items-center justify-between gap-4 text-[12.5px]"
                          >
                            <span className="font-mono text-ink-4">{field}</span>
                            <span className="font-mono text-ink-2">{formatTol(tol)}</span>
                          </div>
                        ),
                      )}
                    </div>
                  </div>
                ) : (
                  <Row label="Epsilon (ε)">
                    <span className="font-mono text-ink-2">{String(oracle.epsilon)}</span>
                  </Row>
                ))}
            </div>
          </Section>
        )}

        {/* operator brief — the steer given to the Task Matrix agent at creation */}
        {context.task_brief && (
          <Section title="Brief">
            <p className="whitespace-pre-wrap rounded-lg border border-line bg-bg-2/40 px-3 py-2 text-[13px] leading-relaxed text-ink-2">
              {context.task_brief}
            </p>
          </Section>
        )}

        {/* sweep config — the agents + band chosen for this run (New Run → Advanced options).
            Config keys stay "difficulty"/"full"; display names are smoke/frontier (ADR-0040). */}
        {context.run_config && (
          <Section title="Sweep config">
            <div className="space-y-3">
              <SweepStage label="Smoke" stage={context.run_config.difficulty} />
              <SweepStage label="Frontier" stage={context.run_config.full} />
            </div>
          </Section>
        )}

        {/* harden trajectory — the harden-review auditor's evidence (only once a harden has fired) */}
        {context.harden_history && context.harden_history.length > 0 && (
          <Section title="Harden trajectory">
            <div className="space-y-1.5">
              {context.harden_history.map((h, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between gap-3 rounded-lg bg-bg-2/40 px-3 py-1.5 text-[12.5px]"
                >
                  <span className="text-ink-3">
                    gen {h.generation ?? i} · {h.stage ?? "—"}
                  </span>
                  <span className="flex items-center gap-2">
                    <span className="font-mono text-ink-2">
                      pass@1 {typeof h.pass_at_1 === "number" ? h.pass_at_1.toFixed(2) : "—"}
                    </span>
                    <Badge tone={h.verdict === "drop" ? "danger" : "warn"}>{h.verdict ?? "harden"}</Badge>
                  </span>
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* sweeps (one row per sweep present) — hidden until the first sweep launches */}
        {sweepEntries.length > 0 && (
          <Section title="Sweeps" icon={<Activity className="size-3.5" />}>
            <div className="space-y-3">
              {sweepEntries.map(([key, sweep]) => (
                <SweepRow key={key} label={sweepLabel(key)} sweep={sweep} />
              ))}
            </div>
          </Section>
        )}
      </CardBody>
    </Card>
  );
}

/** 0..1 → "100%"; pass-through for anything non-numeric. */
function pct(v: unknown): string {
  if (typeof v === "number") return `${Math.round(v * 100)}%`;
  return String(v);
}

function SweepStage({ label, stage }: { label: string; stage: StageSpec }) {
  return (
    <div className="rounded-lg border border-line bg-bg-2/40 px-3 py-2.5">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[12.5px] font-medium text-ink">{label}</span>
        <span className="font-mono text-[11px] text-ink-4">
          {stage.band.basis === "aggregate" ? "agg" : stage.band.basis}{" "}
          {Math.round((stage.band.min_pass ?? 0) * 100)}–{Math.round((stage.band.max_pass ?? 0) * 100)}%
        </span>
      </div>
      <div className="space-y-1">
        {stage.agents.map((a, i) => (
          <div key={i} className="flex items-center gap-2 text-[12px] text-ink-2">
            <ProviderLogo provider={HARNESS_PROVIDER[a.harness] ?? ""} size={13} className="text-ink-3" />
            <span>{a.harness}</span>
            <span className="text-ink-4">·</span>
            <ProviderLogo provider={providerForModel(a.model)} size={13} className="text-ink-3" />
            <span className="text-ink-3">{a.model.split("/").pop()}</span>
            <span className="ml-auto font-mono text-ink-4">×{a.n_trials}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function SweepRow({
  label,
  sweep,
}: {
  label: string;
  sweep: Record<string, any>;
}) {
  // Surface every quantitative specific present (pass@k, per-family band, fairness gap, auditor
  // verdict, blockers, baseline rewards, errored trials) — not just a single status word.
  const metrics: Array<[string, string]> = [];
  const add = (k: string, v: unknown, fmt: (x: any) => string = String) => {
    if (v !== undefined && v !== null) metrics.push([k, fmt(v)]);
  };
  add("pass@1", sweep.pass_at_1 ?? sweep.claude_code_pass_at_1, pct);
  // N-family map when recorded (generic over the configured harnesses); legacy dual-family
  // fields fill in for pre-families manifests.
  const families = (sweep.families ?? null) as Record<string, number> | null;
  if (families && Object.keys(families).length > 0) {
    for (const [fam, v] of Object.entries(families).sort(([a], [b]) => a.localeCompare(b))) {
      add(fam, v, pct);
    }
  } else {
    add("claude-code", sweep.claude_code, pct);
    add("codex", sweep.codex, pct);
  }
  // "aggregate" is the BEST family (the max), not the pooled rate — the gating signal (ADR-0024:
  // a task is too easy if ANY family aces it). Labeled so it's not misread as the combined average.
  add("aggregate (best family)", sweep.aggregate, pct);
  // fairness gap = max pairwise |a − b| across measured families (flags one family unfairly
  // (dis)advantaged); absent for one-family sweeps.
  add("fairness gap", sweep.fairness_gap, pct);
  add("auditor", sweep.auditor_verdict);
  add("blocker findings", sweep.blocker_findings);
  add("suspicious passes", sweep.suspicious_passes?.length);
  add("oracle reward", sweep.oracle_reward);
  add("nop reward", sweep.nop_reward);
  add("errored trials", sweep.n_errored);
  if (sweep.verdict) add("verdict", sweep.verdict);
  if (!metrics.length && sweep.status) add("status", sweep.status);

  const statusTone =
    sweep.status === "running"
      ? "info"
      : sweep.verdict === "harden" || sweep.status === "errored"
        ? "warn"
        : null;

  return (
    <div className="rounded-lg border border-line bg-bg-2/40 px-3 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-[13px] font-medium text-ink">
          {label} sweep
          {statusTone && <Badge tone={statusTone as any}>{sweep.status ?? sweep.verdict}</Badge>}
        </span>
        {sweep.experiment ? (
          <span className="font-mono text-[12.5px] text-ink-3" title="Sweep handle">
            {String(sweep.experiment)}
          </span>
        ) : (
          <span className="text-[12px] italic text-ink-4">no sweep yet</span>
        )}
      </div>
      {metrics.length > 0 && (
        <div className="mt-2 space-y-1.5">
          {metrics.map(([k, v]) => (
            <Row key={k} label={k}>
              <span className="font-mono text-ink-2">{v}</span>
            </Row>
          ))}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="mb-2.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-4">
        {icon}
        {title}
      </div>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 text-[13px]">
      <span className="text-ink-3">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-[13px] italic text-ink-4">{children}</p>;
}

/** Render a per-field tolerance object `{rel?, abs?, exact?}` (or a scalar) as readable text. */
function formatTol(tol: unknown): string {
  if (tol == null) return "—";
  if (typeof tol !== "object") return String(tol);
  const t = tol as { rel?: number | null; abs?: number | null; exact?: boolean };
  if (t.exact) return "exact";
  const bits: string[] = [];
  if (t.rel != null) bits.push(`rel ${t.rel}`);
  if (t.abs != null) bits.push(`abs ${t.abs}`);
  return bits.join(" / ") || "—";
}
