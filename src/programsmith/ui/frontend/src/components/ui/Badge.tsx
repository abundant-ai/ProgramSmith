import type { ReactNode } from "react";
import Chip, { type ChipProps } from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import { cn } from "../../lib/cn";
import type { RunStatus, Stage } from "../../api";
import { stageLabel } from "../../lib/pipeline";

type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "accent" | "human";

const tones: Record<Tone, string> = {
  neutral: "bg-surface-2 text-ink-2 border-line",
  ok: "bg-ok-soft/40 text-ok border-ok/30",
  warn: "bg-warn-soft/40 text-warn border-warn/30",
  danger: "bg-danger-soft/40 text-danger border-danger/30",
  info: "bg-accent-soft/40 text-info border-info/30",
  accent: "bg-accent-soft/40 text-accent border-accent/30",
  human: "bg-accent-soft/40 text-human border-human/30",
};

export function Badge({
  tone = "neutral",
  className,
  children,
  ...rest
}: { tone?: Tone; children: ReactNode } & Omit<ChipProps, "label" | "color" | "children">) {
  return (
    <Chip
      size="small"
      variant="outlined"
      className={cn(tones[tone], className)}
      label={<span className="inline-flex items-center gap-1.5">{children}</span>}
      {...rest}
    />
  );
}

const STATUS_TONE: Record<string, Tone> = {
  in_progress: "info",
  draft: "ok",
  done: "ok",
  accepted: "ok",
  dropped: "neutral",
  blocked: "danger",
  easy: "warn",
};

export function StatusBadge({
  status,
  stage,
  active = false,
  screenedOut = false,
}: {
  status: RunStatus;
  stage?: Stage;
  active?: boolean;
  screenedOut?: boolean;
}) {
  let label = status.replace(/_/g, " ");
  let tone: Tone = STATUS_TONE[status] ?? "neutral";
  if (screenedOut) {
    label = "screened out";
    tone = "neutral";
  } else if (status === "in_progress" && stage) {
    label = stageLabel(stage);
    tone = "info";
  } else if (status === "done") {
    label = "exported";
    tone = "ok";
  } else if (status === "draft") {
    label = "draft";
    tone = "ok";
  } else if (status === "easy") {
    label = "easy shelf";
    tone = "warn";
  }
  const working = status === "in_progress" && active;
  return (
    <Badge tone={tone} className="capitalize">
      {working ? (
        <CircularProgress color="inherit" size={11} />
      ) : (
        <span
          className={cn("size-1.5", status === "in_progress" && "animate-pulse")}
          style={{ background: "currentColor" }}
        />
      )}
      {label}
    </Badge>
  );
}
