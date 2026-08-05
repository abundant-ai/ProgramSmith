import { useEffect, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { motion } from "framer-motion";
import {
  Check,
  Info,
  KeyRound,
  Pencil,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Wrench,
} from "lucide-react";
import { api, type Settings } from "../api";
import { usePreflight } from "../lib/PreflightContext";
import { Card, CardBody, CardHeader, CardTitle } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Field, Input, Select } from "../components/ui/Field";
import { PreflightChecks } from "../components/PreflightChecks";
import { Skeleton } from "../components/ui/Skeleton";

type Saved = "idle" | "saving" | "ok" | "error";
type SecretField =
  | "claude_code_oauth_token"
  | "anthropic_api_key"
  | "openai_api_key"
  | "gemini_api_key"
  | "zai_api_key";

const MODEL_OPTIONS: { value: string; label: string }[] = [
  { value: "claude-sonnet-5", label: "Sonnet 5" },
  { value: "claude-sonnet-4-6", label: "Sonnet 4.6" },
  { value: "claude-opus-4-8", label: "Opus 4.8" },
  { value: "claude-haiku-4-5-20251001", label: "Haiku 4.5" },
];

export function SettingsPage() {
  const location = useLocation();
  const firstRun = (location.state as { firstRun?: boolean } | null)?.firstRun;
  const { preflight, loading: preLoading, refresh: refreshPreflight } = usePreflight();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<Saved>("idle");
  const [secretMasks, setSecretMasks] = useState<Partial<Record<SecretField, string | null>>>({});
  const [secretValues, setSecretValues] = useState<Partial<Record<SecretField, string>>>({});
  const [savingSecret, setSavingSecret] = useState<SecretField | null>(null);

  // form state
  const [model, setModel] = useState("");
  const [modelLight, setModelLight] = useState("");
  const [modelAnalysis, setModelAnalysis] = useState("");
  const [runsDir, setRunsDir] = useState("");
  const [ciRepo, setCiRepo] = useState("");
  const [dropAfter, setDropAfter] = useState("");
  const [minImprove, setMinImprove] = useState("");
  const [agenticConc, setAgenticConc] = useState("");
  const [outboxDir, setOutboxDir] = useState("");
  const [authorName, setAuthorName] = useState("");
  const [authorEmail, setAuthorEmail] = useState("");
  const [authorOrg, setAuthorOrg] = useState("");

  function hydrate(s: Settings) {
    setModel(s.default_cell_model ?? "");
    setModelLight(s.cell_model_light ?? "");
    setModelAnalysis(s.cell_model_analysis ?? "");
    setRunsDir(s.runs_dir ?? "");
    setCiRepo(s.ci_repo_root ?? "");
    setDropAfter(s.harden_drop_after != null ? String(s.harden_drop_after) : "");
    setMinImprove(s.harden_min_improvement != null ? String(s.harden_min_improvement) : "");
    setAgenticConc(s.agentic_concurrency != null ? String(s.agentic_concurrency) : "");
    setOutboxDir(s.outbox_dir ?? "");
    setAuthorName(s.author_name ?? "");
    setAuthorEmail(s.author_email ?? "");
    setAuthorOrg(s.author_organization ?? "");
    setSecretMasks({
      claude_code_oauth_token: s.claude_code_oauth_token,
      anthropic_api_key: s.anthropic_api_key,
      openai_api_key: s.openai_api_key,
      gemini_api_key: s.gemini_api_key,
      zai_api_key: s.zai_api_key,
    });
  }

  async function saveSecret(field: SecretField, clear = false) {
    const value = clear ? "" : (secretValues[field] ?? "").trim();
    if (!clear && !value) return;
    setSavingSecret(field);
    setError(null);
    try {
      const updated = await api.saveSettings({ [field]: value });
      hydrate(updated);
      setSecretValues((current) => ({ ...current, [field]: "" }));
      void refreshPreflight();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingSecret(null);
    }
  }

  useEffect(() => {
    let active = true;
    api
      .getSettings()
      .then((s) => active && hydrate(s))
      .catch((e) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  async function save() {
    setSaved("saving");
    setError(null);
    const num = (v: string) => (v.trim() === "" ? undefined : Number(v));
    try {
      const body: Partial<Settings> = {
        default_cell_model: model || undefined,
        cell_model_light: modelLight || undefined,
        cell_model_analysis: modelAnalysis || undefined,
        runs_dir: runsDir || undefined,
        ci_repo_root: ciRepo || undefined,
        harden_drop_after: num(dropAfter),
        harden_min_improvement: num(minImprove),
        agentic_concurrency: num(agenticConc),
        outbox_dir: outboxDir || undefined,
        author_name: authorName || undefined,
        author_email: authorEmail || undefined,
        author_organization: authorOrg || undefined,
      };
      const updated = await api.saveSettings(body);
      hydrate(updated);
      setSaved("ok");
      void refreshPreflight();
      window.setTimeout(() => setSaved("idle"), 2200);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaved("error");
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-7">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          {firstRun ? "Welcome — let's get set up" : "Settings"}
        </h1>
      </div>

      {/* Preflight */}
      <Card>
        <CardHeader>
          <CardTitle>
            <ShieldCheck className="size-4 text-ink-3" />
            Preflight
          </CardTitle>
          <Button size="sm" variant="ghost" onClick={() => void refreshPreflight()}>
            <RefreshCw className="size-3.5" />
            Recheck
          </Button>
        </CardHeader>
        <CardBody className="space-y-4">
          {preflight && !preflight.ready && (
            <div className="flex items-center gap-2 rounded-xl bg-warn-soft/25 px-4 py-2.5 text-sm font-medium text-warn">
              <Info className="size-4" />
              Setup incomplete — resolve the flagged checks below.
            </div>
          )}
          <PreflightChecks preflight={preflight} loading={preLoading} />
        </CardBody>
      </Card>

      {/* Credentials — locally persisted owner-only, and never returned raw to the browser. */}
      <Card>
        <CardHeader>
          <CardTitle>
            <KeyRound className="size-4 text-ink-3" />
            Credentials
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <p className="text-[13px] leading-relaxed text-ink-2">
            Claude CLI login works automatically. Saved keys are local and masked; environment
            variables take precedence.
          </p>
          <div className="divide-y divide-line rounded-xl border border-line">
            <CredentialRow label="Claude OAuth token" hint={<>run <code>claude setup-token</code></>} field="claude_code_oauth_token" required mask={secretMasks.claude_code_oauth_token} value={secretValues.claude_code_oauth_token ?? ""} busy={savingSecret === "claude_code_oauth_token"} onChange={(value) => setSecretValues((s) => ({ ...s, claude_code_oauth_token: value }))} onSave={(clear) => void saveSecret("claude_code_oauth_token", clear)} />
            <CredentialRow label="Anthropic API key" field="anthropic_api_key" required mask={secretMasks.anthropic_api_key} value={secretValues.anthropic_api_key ?? ""} busy={savingSecret === "anthropic_api_key"} onChange={(value) => setSecretValues((s) => ({ ...s, anthropic_api_key: value }))} onSave={(clear) => void saveSecret("anthropic_api_key", clear)} />
            <CredentialRow label="OpenAI API key" field="openai_api_key" mask={secretMasks.openai_api_key} value={secretValues.openai_api_key ?? ""} busy={savingSecret === "openai_api_key"} onChange={(value) => setSecretValues((s) => ({ ...s, openai_api_key: value }))} onSave={(clear) => void saveSecret("openai_api_key", clear)} />
            <CredentialRow label="Gemini API key" field="gemini_api_key" mask={secretMasks.gemini_api_key} value={secretValues.gemini_api_key ?? ""} busy={savingSecret === "gemini_api_key"} onChange={(value) => setSecretValues((s) => ({ ...s, gemini_api_key: value }))} onSave={(clear) => void saveSecret("gemini_api_key", clear)} />
            <CredentialRow label="Z.ai API key" field="zai_api_key" mask={secretMasks.zai_api_key} value={secretValues.zai_api_key ?? ""} busy={savingSecret === "zai_api_key"} onChange={(value) => setSecretValues((s) => ({ ...s, zai_api_key: value }))} onSave={(clear) => void saveSecret("zai_api_key", clear)} />
          </div>
        </CardBody>
      </Card>

      {loading ? (
        <Card>
          <CardBody className="space-y-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </CardBody>
        </Card>
      ) : (
        <>
          {/* Output: the pipeline runs fully automatic end to end; accepted tasks land in the outbox. */}
          <Card>
            <CardHeader>
              <CardTitle>
                <ShieldCheck className="size-4 text-ink-3" />
                Output
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-5">
              <Field
                label="Outbox directory"
                htmlFor="outbox_dir"
                hint="Exports are sorted into tasks/, drafts/, and easy/."
              >
                <Input
                  id="outbox_dir"
                  placeholder="out"
                  value={outboxDir}
                  onChange={(e) => setOutboxDir(e.target.value)}
                />
              </Field>
            </CardBody>
          </Card>

          {/* Execution */}
          <Card>
            <CardHeader>
              <CardTitle>
                <Wrench className="size-4 text-ink-3" />
                Execution
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-5">
              {/* Cell model routing: heavy / light / analysis */}
              <div className="grid gap-5 sm:grid-cols-2">
                <Field label="Generation model" htmlFor="model" hint="Oracle, task creation, and revisions.">
                  <ModelSelect id="model" value={model} onChange={setModel} />
                </Field>
                <Field label="Matrix model" htmlFor="model_light" hint="Candidate selection.">
                  <ModelSelect id="model_light" value={modelLight} onChange={setModelLight} />
                </Field>
                <Field label="Analysis model" htmlFor="model_analysis" hint="Failure audits.">
                  <ModelSelect id="model_analysis" value={modelAnalysis} onChange={setModelAnalysis} />
                </Field>
              </div>
              <Field label="Runs directory" htmlFor="runs_dir">
                <Input id="runs_dir" placeholder="/path/to/runs" value={runsDir} onChange={(e) => setRunsDir(e.target.value)} />
              </Field>
              <Field label="Static checks override" htmlFor="ci_repo">
                <Input id="ci_repo" placeholder="Bundled checks (default)" value={ciRepo} onChange={(e) => setCiRepo(e.target.value)} />
              </Field>
            </CardBody>
          </Card>

          {/* Task authorship — stamped into every generated task.toml */}
          <Card>
            <CardHeader>
              <CardTitle>
                <Pencil className="size-4 text-ink-3" />
                Task authorship
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-5">
              <div className="grid gap-5 sm:grid-cols-3">
                <Field label="Author name" htmlFor="author_name">
                  <Input id="author_name" placeholder="Your name" value={authorName} onChange={(e) => setAuthorName(e.target.value)} />
                </Field>
                <Field label="Author email" htmlFor="author_email">
                  <Input id="author_email" placeholder="you@example.com" value={authorEmail} onChange={(e) => setAuthorEmail(e.target.value)} />
                </Field>
                <Field label="Organization" htmlFor="author_org">
                  <Input id="author_org" placeholder="(optional)" value={authorOrg} onChange={(e) => setAuthorOrg(e.target.value)} />
                </Field>
              </div>
            </CardBody>
          </Card>

          {/* Pipeline tuning */}
          <Card>
            <CardHeader>
              <CardTitle>
                <SlidersHorizontal className="size-4 text-ink-3" />
                Pipeline tuning
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-5">
              <div className="grid gap-5 sm:grid-cols-2">
                <Field label="Harden attempts" htmlFor="drop_after" hint="Drop after this many revisions without enough improvement.">
                  <Input id="drop_after" type="number" min={1} placeholder="2" value={dropAfter} onChange={(e) => setDropAfter(e.target.value)} />
                </Field>
                <Field label="Minimum improvement" htmlFor="min_improve" hint="Required pass@1 reduction per revision (0–1).">
                  <Input id="min_improve" type="number" step="0.01" min={0} max={1} placeholder="0.10" value={minImprove} onChange={(e) => setMinImprove(e.target.value)} />
                </Field>
                <Field label="Concurrent agents" htmlFor="agentic_conc">
                  <Input id="agentic_conc" type="number" min={1} placeholder="1" value={agenticConc} onChange={(e) => setAgenticConc(e.target.value)} />
                </Field>
              </div>
            </CardBody>
          </Card>

          {error && (
            <div className="rounded-xl border border-danger/30 bg-danger-soft/20 px-4 py-2.5 text-sm text-danger">
              {error}
            </div>
          )}

          <div className="flex items-center gap-3">
            <Button variant="primary" onClick={() => void save()} loading={saved === "saving"}>
              Save settings
            </Button>
            {saved === "ok" && (
              <motion.span
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                className="flex items-center gap-1.5 text-sm font-medium text-ok"
              >
                <Check className="size-4" />
                Saved
              </motion.span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function CredentialRow({
  label,
  hint,
  field,
  required = false,
  mask,
  value,
  busy,
  onChange,
  onSave,
}: {
  label: string;
  hint?: ReactNode;
  field: SecretField;
  required?: boolean;
  mask?: string | null;
  value: string;
  busy: boolean;
  onChange: (value: string) => void;
  onSave: (clear: boolean) => void;
}) {
  return (
    <div className="space-y-2 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <label htmlFor={field} className="text-[13px] font-medium text-ink">
            {label}
          </label>
          {hint && <p className="text-[11.5px] text-ink-4">{hint}</p>}
        </div>
        <span className={mask ? "text-[11.5px] font-mono text-ok" : "text-[11.5px] text-ink-4"}>
          {mask ? `configured ${mask}` : required ? "required (one of two)" : "not configured"}
        </span>
      </div>
      <div className="flex gap-2">
        <Input
          id={field}
          type="password"
          autoComplete="off"
          placeholder={mask ? "Enter a new value to replace" : "Paste credential"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && value.trim() && onSave(false)}
        />
        <Button variant="secondary" onClick={() => onSave(false)} loading={busy} disabled={!value.trim()}>
          Save
        </Button>
        {mask && (
          <Button variant="ghost" onClick={() => onSave(true)} disabled={busy}>
            Clear
          </Button>
        )}
      </div>
    </div>
  );
}

/** One cell-model dropdown: known options + a pass-through entry for whatever non-catalog model the
 *  config currently holds, so saving never silently rewrites it. */
function ModelSelect({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
      {!value && <option value="">Select a model…</option>}
      {value && !MODEL_OPTIONS.some((o) => o.value === value) && (
        <option value={value}>{value}</option>
      )}
      {MODEL_OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </Select>
  );
}
