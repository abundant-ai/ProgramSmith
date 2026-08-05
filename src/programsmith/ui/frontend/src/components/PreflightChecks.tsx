import { Check, RefreshCw, X } from "lucide-react";
import { motion } from "framer-motion";
import type { Preflight, PreflightCheck } from "../api";
import { cn } from "../lib/cn";
import { Skeleton } from "./ui/Skeleton";

/** Friendly labels for known check names; anything the backend adds falls back to its raw name. */
const LABELS: Record<string, string> = {
  github: "GitHub access",
  claude_oauth: "Claude credentials",
  docker: "Docker",
  anthropic_cred: "Anthropic",
  claude_cli: "Claude Code CLI",
  disk: "Disk",
};

const isOptionalProvider = (check: PreflightCheck) => check.name.endsWith("_optional");

function compactDetail(check: PreflightCheck): string {
  if (!check.ok) return check.detail;
  if (check.name === "docker") return "Running";
  if (check.name === "anthropic_cred") {
    const detail = check.detail.toLowerCase();
    if (detail.includes("keychain")) return "Claude CLI keychain";
    if (detail.includes("oauth")) return "OAuth token";
    if (detail.includes("api key")) return "API key";
  }
  return check.detail;
}

export function PreflightChecks({
  preflight,
  loading,
}: {
  preflight: Preflight | null;
  loading: boolean;
}) {
  if (loading && !preflight) {
    return (
      <div className="grid gap-2 sm:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  }

  // Optional solver credentials belong in the Credentials section. They are capabilities, not
  // readiness requirements, and showing missing providers as green checks makes preflight noisy.
  const checks = (preflight?.checks ?? []).filter((check) => !isOptionalProvider(check));
  if (checks.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-line bg-surface-2 px-4 py-3 text-sm text-ink-3">
        <RefreshCw className="size-4" />
        No preflight data available.
      </div>
    );
  }

  return (
    <div className="grid gap-2 sm:grid-cols-3">
      {checks.map((c, i) => (
        <motion.div
          key={c.name}
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.05 }}
          className={cn(
            "flex min-w-0 items-center gap-2.5 rounded-lg border px-3 py-2.5",
            c.ok
              ? "border-ok/20 bg-ok-soft/15"
              : "border-danger/25 bg-danger-soft/15",
          )}
        >
          <span
            className={cn(
              "flex size-5 shrink-0 items-center justify-center rounded-full",
              c.ok ? "bg-ok/20 text-ok" : "bg-danger/20 text-danger",
            )}
          >
            {c.ok ? <Check className="size-3.5" /> : <X className="size-3.5" />}
          </span>
          <div className="min-w-0">
            <div className="text-[12.5px] font-medium leading-tight text-ink">
              {LABELS[c.name] ?? c.name}
            </div>
            {c.detail && (
              <div className="mt-1 truncate text-[11.5px] leading-tight text-ink-3" title={c.detail}>
                {compactDetail(c)}
              </div>
            )}
          </div>
        </motion.div>
      ))}
    </div>
  );
}
