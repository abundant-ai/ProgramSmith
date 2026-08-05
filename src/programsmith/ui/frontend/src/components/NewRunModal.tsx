import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Collapse from "@mui/material/Collapse";
import {
  ChevronRight,
  GitFork,
  Plus,
  Save,
  Sparkles,
  Trash2,
} from "lucide-react";
import {
  api,
  ApiError,
  type Catalog,
  type ModelBand,
  type RunConfig,
  type StageSpec,
} from "../api";
import { Modal } from "./ui/Modal";
import { Button } from "./ui/Button";
import { Field, Input, Select } from "./ui/Field";
import { ProviderLogo } from "./Logos";

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));

export function NewRunModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const navigate = useNavigate();
  const [repo, setRepo] = useState("");
  const [sha, setSha] = useState("");
  const [slug, setSlug] = useState("");
  const [brief, setBrief] = useState("");
  const [mode, setMode] = useState<"full" | "draft">("full");
  const [cellModel, setCellModel] = useState("claude-sonnet-5");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [cfg, setCfg] = useState<RunConfig | null>(null);
  const [advanced, setAdvanced] = useState(
    typeof window !== "undefined" && window.location.hash.includes("adv"),
  );
  const [presets, setPresets] = useState<Record<string, RunConfig>>({});
  const [presetName, setPresetName] = useState("");

  useEffect(() => {
    if (!open) return;
    api.catalog().then((c) => {
      setCatalog(c);
      setCfg((prev) => prev ?? clone(c.default_config));
    });
    api.listPresets().then((r) => setPresets(r.presets)).catch(() => {});
  }, [open]);

  function reset() {
    setRepo("");
    setSha("");
    setSlug("");
    setBrief("");
    setMode("full");
    setCellModel("claude-sonnet-5");
    setError(null);
    setSubmitting(false);
    setPresetName("");
    if (catalog) setCfg(clone(catalog.default_config));
  }
  function close() {
    reset();
    onClose();
  }

  async function submit() {
    if (!repo.trim()) {
      setError("A source repository is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.createRun({
        repo: repo.trim(),
        sha: sha.trim() || undefined,
        slug: slug.trim() || undefined,
        brief: brief.trim() || undefined,
        config: cfg ?? undefined,
        mode,
        cell_model: cellModel,
      });
      onCreated();
      reset();
      onClose();
      navigate(`/run/${encodeURIComponent(result.key)}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError("A run with that name already exists. Choose a different name.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
      setSubmitting(false);
    }
  }

  async function savePreset() {
    if (!presetName.trim() || !cfg) return;
    try {
      const r = await api.savePreset(presetName.trim(), cfg);
      setPresets(r.presets);
      setPresetName("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="New run"
      style={{
        maxWidth: advanced ? "62rem" : "34rem",
        transition: "max-width 260ms cubic-bezier(0.4, 0, 0.2, 1)",
      }}
    >
      <div className="space-y-5">
        <Field label="Source repository" htmlFor="repo" hint="owner/name or a full GitHub URL.">
          <div className="relative">
            <GitFork className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-4" />
            <Input
              id="repo"
              className="pl-9"
              autoFocus
              placeholder="devkit/minpack"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !advanced && void submit()}
            />
          </div>
        </Field>

        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Pinned SHA" htmlFor="sha" hint="Resolves HEAD if blank.">
            <Input id="sha" className="font-mono" placeholder="(optional)" value={sha} onChange={(e) => setSha(e.target.value)} />
          </Field>
          <Field label="Run name" htmlFor="slug" hint="Defaults to the repo name.">
            <Input id="slug" placeholder="(optional)" value={slug} onChange={(e) => setSlug(e.target.value)} />
          </Field>
        </div>

        <Field
          label="Task brief (optional)"
          htmlFor="brief"
        >
          <textarea
            id="brief"
            rows={3}
            placeholder="e.g. Scope the flag surface to the core subcommands; skip the network-dependent modes; prefer stdin-driven cases."
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            className="w-full resize-y rounded-xl border border-line bg-bg-2/60 px-3.5 py-2.5 text-sm text-ink placeholder:text-ink-4 transition-colors focus-ring focus:border-accent/50"
          />
        </Field>

        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Pipeline mode" htmlFor="pipeline_mode" hint="Draft exports immediately after Static CI.">
            <Select id="pipeline_mode" value={mode} onChange={(e) => setMode(e.target.value as "full" | "draft")}>
              <option value="full">Full — calibrate and evaluate</option>
              <option value="draft">Draft — stop after Static CI</option>
            </Select>
          </Field>
          <Field label="Task-generation model" htmlFor="cell_model">
            <Select id="cell_model" value={cellModel} onChange={(e) => setCellModel(e.target.value)}>
              <option value="claude-sonnet-5">Sonnet 5</option>
              <option value="claude-opus-4-8">Opus 4.8</option>
              <option value="claude-sonnet-4-6">Sonnet 4.6</option>
            </Select>
          </Field>
        </div>

        {/* Advanced options — collapsed by default, prefilled with the defaults */}
        <div className="rounded-xl border border-line">
          <button
            onClick={() => setAdvanced((v) => !v)}
            className="focus-ring flex w-full items-center justify-between gap-2 rounded-xl px-4 py-3 text-left"
          >
            <span className="flex items-center gap-2 text-[13px] font-medium text-ink-2">
              <ChevronRight
                className={`size-4 text-ink-4 transition-transform duration-200 ${advanced ? "rotate-90" : ""}`}
              />
              Advanced options
            </span>
          </button>

          <Collapse in={advanced} timeout={260} unmountOnExit>
            {catalog && cfg && <div className="space-y-5 border-t border-line p-4">
              {/* presets */}
              <div className="flex flex-wrap items-end gap-2">
                <Field label="Preset" htmlFor="preset">
                  <Select
                    id="preset"
                    value=""
                    onChange={(e) => {
                      const p = presets[e.target.value];
                      if (p) setCfg(clone(p));
                    }}
                  >
                    <option value="">Load preset…</option>
                    {Object.keys(presets).map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </Select>
                </Field>
                <div className="flex items-center gap-2">
                  <Input
                    placeholder="Save as…"
                    value={presetName}
                    className="w-36"
                    onChange={(e) => setPresetName(e.target.value)}
                  />
                  <Button variant="secondary" onClick={() => void savePreset()} disabled={!presetName.trim()}>
                    <Save className="size-3.5" />
                    Save
                  </Button>
                </div>
              </div>

              {/* Stage keys stay "difficulty"/"full" (persisted names); the semantics are
                  smoke (cheap model) and frontier (strongest model). */}
              <StageEditor
                title="Smoke sweep"
                hint="Default: smoke model ×3, band 0–90% — with k=3 only 3/3 saturates."
                catalog={catalog}
                stage={cfg.difficulty}
                onChange={(s) => setCfg({ ...cfg, difficulty: s })}
              />
              <StageEditor
                title="Frontier sweep"
                hint="Default: frontier model ×3, band 30–70% — the 1/3–2/3 target window."
                catalog={catalog}
                stage={cfg.full}
                onChange={(s) => setCfg({ ...cfg, full: s })}
              />
            </div>}
          </Collapse>
        </div>

        {error && (
          <div className="rounded-xl border border-danger/30 bg-danger-soft/20 px-4 py-2.5 text-sm text-danger">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2.5 pt-1">
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={submitting}>
            <Sparkles className="size-4" />
            Create run
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/* ---- one sweep stage: agent rows + band ------------------------------------ */
function StageEditor({
  title,
  hint,
  catalog,
  stage,
  onChange,
}: {
  title: string;
  /** One-line note under the title describing the shipped default agents + band. */
  hint?: string;
  catalog: Catalog;
  stage: StageSpec;
  onChange: (s: StageSpec) => void;
}) {
  const harnessKeys = Object.keys(catalog.harnesses);
  const modelKeys = Object.keys(catalog.models);

  const setAgent = (i: number, patch: Partial<StageSpec["agents"][number]>) =>
    onChange({ ...stage, agents: stage.agents.map((a, j) => (j === i ? { ...a, ...patch } : a)) });
  const addAgent = () =>
    onChange({
      ...stage,
      agents: [...stage.agents, { harness: harnessKeys[0], model: modelKeys[0], n_trials: 3 }],
    });
  const removeAgent = (i: number) =>
    onChange({ ...stage, agents: stage.agents.filter((_, j) => j !== i) });

  const basisChoices = ["aggregate", ...Array.from(new Set(stage.agents.map((a) => a.harness)))];
  const basis = basisChoices.includes(stage.band.basis) ? stage.band.basis : "aggregate";
  const basisK =
    basis === "aggregate"
      ? Math.max(0, ...stage.agents.map((a) => a.n_trials))
      : (stage.agents.find((a) => a.harness === basis)?.n_trials ?? 0);
  const pct = (v: number) => Math.round(v * 100);

  // per-model acceptance (combinator any/all) — harness choices come from configured agents,
  // falling back to the full catalog when the stage has none yet.
  const combinator = stage.band.combinator ?? "aggregate";
  const perModel = stage.band.per_model ?? [];
  const harnessOptions = Array.from(new Set(stage.agents.map((a) => a.harness)));
  const modelBasisChoices = harnessOptions.length > 0 ? harnessOptions : harnessKeys;
  const setBand = (patch: Partial<StageSpec["band"]>) =>
    onChange({ ...stage, band: { ...stage.band, ...patch } });
  const setPerModel = (i: number, patch: Partial<ModelBand>) =>
    setBand({ per_model: perModel.map((m, j) => (j === i ? { ...m, ...patch } : m)) });
  const addPerModel = () =>
    setBand({
      // 0.70 = the frontier saturation ceiling (ADR-0040: keep window 30–70%)
      per_model: [...perModel, { basis: modelBasisChoices[0], min_pass: 0, max_pass: 0.7 }],
    });
  const removePerModel = (i: number) =>
    setBand({ per_model: perModel.filter((_, j) => j !== i) });

  return (
    <div className="space-y-2.5">
      <div>
        <h4 className="text-[13px] font-semibold text-ink">{title}</h4>
        {hint && <p className="mt-0.5 text-[11.5px] leading-snug text-ink-4">{hint}</p>}
      </div>

      <div className="space-y-2">
        {stage.agents.map((a, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-lg border border-line bg-bg-2/40 px-2 py-1">
              <ProviderLogo provider={catalog.harnesses[a.harness]?.provider ?? ""} className="shrink-0 text-ink-3" />
              <Select
                value={a.harness}
                onChange={(e) => setAgent(i, { harness: e.target.value })}
                className="h-8 border-0 bg-transparent px-1 text-[12.5px]"
              >
                {harnessKeys.map((h) => (
                  <option key={h} value={h}>
                    {catalog.harnesses[h].label}
                    {catalog.harnesses[h].recommended === false ? " — not recommended" : ""}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-lg border border-line bg-bg-2/40 px-2 py-1">
              <ProviderLogo provider={catalog.models[a.model]?.provider ?? ""} className="shrink-0 text-ink-3" />
              <Select
                value={a.model}
                onChange={(e) => setAgent(i, { model: e.target.value })}
                className="h-8 border-0 bg-transparent px-1 text-[12.5px]"
              >
                {modelKeys.map((m) => (
                  <option key={m} value={m}>
                    {catalog.models[m].label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex items-center gap-1">
              <Input
                type="number"
                min={1}
                max={50}
                value={a.n_trials}
                onChange={(e) => setAgent(i, { n_trials: Math.max(1, Number(e.target.value) || 1) })}
                className="h-9 w-14 text-center"
                title="trials (k in pass@k)"
              />
              <span className="text-[11px] text-ink-4">×</span>
            </div>
            <button
              onClick={() => removeAgent(i)}
              disabled={stage.agents.length <= 1}
              className="focus-ring rounded-md p-1.5 text-ink-4 transition-colors hover:bg-surface-2 hover:text-danger disabled:opacity-30"
              title="Remove agent"
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        ))}
        <button
          onClick={addAgent}
          className="focus-ring inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[12.5px] font-medium text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <Plus className="size-3.5" />
          Add agent
        </button>
      </div>

      {/* band */}
      <div className="space-y-2.5 rounded-lg border border-line bg-bg-2/30 px-3 py-2.5">
        <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
          <div className="space-y-1">
            <label className="block text-[11px] font-medium uppercase tracking-[0.06em] text-ink-4">
              Acceptance
            </label>
            <Select
              value={combinator}
              onChange={(e) => setBand({ combinator: e.target.value })}
              className="h-8 w-52 text-[12.5px]"
            >
              <option value="aggregate">Aggregate (best model)</option>
              <option value="any">Any model hard (sellable)</option>
              <option value="all">All models hard</option>
            </Select>
          </div>

          {combinator === "aggregate" && (
            <>
              <div className="space-y-1">
                <label className="block text-[11px] font-medium uppercase tracking-[0.06em] text-ink-4">
                  Band basis
                </label>
                <Select
                  value={basis}
                  onChange={(e) => setBand({ basis: e.target.value })}
                  className="h-8 w-40 text-[12.5px]"
                >
                  <option value="aggregate">Aggregate (best agent)</option>
                  {Array.from(new Set(stage.agents.map((a) => a.harness))).map((h) => (
                    <option key={h} value={h}>{catalog.harnesses[h]?.label ?? h}</option>
                  ))}
                </Select>
              </div>
              <div className="space-y-1">
                <label className="block text-[11px] font-medium uppercase tracking-[0.06em] text-ink-4">
                  Target pass@{basisK || "k"}
                </label>
                <div className="flex items-center gap-1.5 text-[12.5px]">
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    value={pct(stage.band.min_pass)}
                    onChange={(e) => setBand({ min_pass: (Number(e.target.value) || 0) / 100 })}
                    className="h-8 w-16 text-center"
                  />
                  <span className="text-ink-4">–</span>
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    value={pct(stage.band.max_pass)}
                    onChange={(e) => setBand({ max_pass: (Number(e.target.value) || 0) / 100 })}
                    className="h-8 w-16 text-center"
                  />
                  <span className="text-ink-4">%</span>
                </div>
              </div>
            </>
          )}
        </div>

        {(combinator === "any" || combinator === "all") && (
          <div className="space-y-2 border-t border-line pt-2.5">
            <div className="space-y-1.5">
              {perModel.length === 0 && (
                <p className="text-[11.5px] text-ink-4">
                  No models yet — add one to define per-model bands.
                </p>
              )}
              {perModel.map((m, i) => {
                const k = stage.agents.find((a) => a.harness === m.basis)?.n_trials ?? 0;
                return (
                  <div key={i} className="flex flex-wrap items-center gap-2">
                    <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-lg border border-line bg-bg-2/40 px-2 py-1">
                      <ProviderLogo
                        provider={catalog.harnesses[m.basis]?.provider ?? ""}
                        className="shrink-0 text-ink-3"
                      />
                      <Select
                        value={m.basis}
                        onChange={(e) => setPerModel(i, { basis: e.target.value })}
                        className="h-8 border-0 bg-transparent px-1 text-[12.5px]"
                      >
                        {modelBasisChoices.map((h) => (
                          <option key={h} value={h}>
                            {catalog.harnesses[h]?.label ?? h}
                          </option>
                        ))}
                      </Select>
                    </div>
                    <div className="flex items-center gap-1.5 text-[12.5px]">
                      <span className="text-[11px] text-ink-4">pass@{k || "k"}</span>
                      <Input
                        type="number"
                        min={0}
                        max={100}
                        step={5}
                        value={pct(m.min_pass)}
                        onChange={(e) => setPerModel(i, { min_pass: (Number(e.target.value) || 0) / 100 })}
                        className="h-8 w-16 text-center"
                      />
                      <span className="text-ink-4">–</span>
                      <Input
                        type="number"
                        min={0}
                        max={100}
                        step={5}
                        value={pct(m.max_pass)}
                        onChange={(e) => setPerModel(i, { max_pass: (Number(e.target.value) || 0) / 100 })}
                        className="h-8 w-16 text-center"
                      />
                      <span className="text-ink-4">%</span>
                    </div>
                    <button
                      onClick={() => removePerModel(i)}
                      className="focus-ring rounded-md p-1.5 text-ink-4 transition-colors hover:bg-surface-2 hover:text-danger"
                      title="Remove model"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                );
              })}
            </div>
            <button
              onClick={addPerModel}
              className="focus-ring inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[12.5px] font-medium text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
            >
              <Plus className="size-3.5" />
              Add model
            </button>
            <p className="text-[11.5px] leading-snug text-ink-4">
              {combinator === "any"
                ? "any = keep if at least one model finds it hard (its pass ≤ its max)."
                : "all = keep only if every listed model finds it hard."}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
