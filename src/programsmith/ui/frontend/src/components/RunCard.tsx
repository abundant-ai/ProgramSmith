import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { Link } from "react-router-dom";
import { GitBranch, Pause } from "lucide-react";
import type { RunSummary } from "../api";
import { StatusBadge } from "./ui/Badge";
import { fmtPassAt1, parsePassAt1 } from "../lib/format";
import { FORWARD_STAGES, stageLabel } from "../lib/pipeline";

function progress(run: RunSummary): number {
  if (typeof run.progress === "number") return run.progress;
  if (run.status === "done") return 1;
  const idx = FORWARD_STAGES.findIndex((item) => item.stage === run.stage);
  if (idx < 0) return ["dropped", "blocked", "easy"].includes(run.status) ? 1 : 0;
  return (idx + 1) / FORWARD_STAGES.length;
}

export function RunCard({ run }: { run: RunSummary; index?: number }) {
  const theme = useTheme();
  const fraction = progress(run);
  const pass = parsePassAt1(run.difficulty_pass_at_1);
  const stage = run.screened_out
    ? "Source screened out"
    : run.status === "draft"
      ? "Static CI passed"
      : stageLabel(run.stage);
  const detail = run.screened_out
    ? "No task was created"
    : run.status === "draft"
      ? "Uncalibrated draft"
    : run.full_sweep_band
      ? `Frontier ${run.full_sweep_band}`
      : pass !== null
        ? `pass@1 ${fmtPassAt1(run.difficulty_pass_at_1)}`
        : "Not calibrated";
  const barColor = run.status === "blocked"
    ? theme.palette.error.main
    : run.status === "done" || run.status === "draft"
      ? theme.palette.success.main
      : "var(--color-node-gate)";

  return (
    <Paper
      component={Link}
      to={`/run/${encodeURIComponent(run.key)}`}
      variant="outlined"
      square
      sx={{
        display: "block",
        p: 2,
        color: "text.primary",
        textDecoration: "none",
        transition: "border-color 120ms ease, background-color 120ms ease",
        "&:hover": { borderColor: "text.secondary", bgcolor: "action.hover" },
        "&:focus-visible": { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: 1 },
      }}
    >
      <Box sx={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 2 }}>
        <Box sx={{ minWidth: 0 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Typography variant="h3" noWrap>{run.slug ?? run.key}</Typography>
            {run.paused && <Pause size={13} color="#8a5b00" />}
          </Box>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ mt: 0.5, display: "flex", alignItems: "center", gap: 0.75, fontFamily: "monospace", fontSize: 11.5 }}
          >
            <GitBranch size={12} />
            <span className="truncate">{run.key}</span>
          </Typography>
        </Box>
        <StatusBadge status={run.status} stage={run.stage} active={!!run.active_job || !!run.waiting} screenedOut={!!run.screened_out} />
      </Box>

      <Box sx={{ mt: 2.25 }}>
        <Box sx={{ mb: 0.75, display: "flex", justifyContent: "space-between", gap: 2 }}>
          <Typography variant="body2" color="text.secondary">{stage}</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ fontVariantNumeric: "tabular-nums" }}>
            {Math.round(fraction * 100)}%
          </Typography>
        </Box>
        <LinearProgress
          variant="determinate"
          value={fraction * 100}
          sx={{ height: 3, bgcolor: "divider", "& .MuiLinearProgress-bar": { bgcolor: barColor } }}
        />
      </Box>

      <Box sx={{ mt: 2, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 2 }}>
        <Typography variant="body2" color="text.secondary">{detail}</Typography>
        {(run.revise > 0 || run.harden > 0 || (run.ease ?? 0) > 0) && (
          <Typography variant="body2" color="text.secondary" sx={{ fontSize: 11.5 }}>
            {run.revise > 0 ? `${run.revise} revise` : run.harden > 0 ? `${run.harden} harden` : `${run.ease} ease`}
          </Typography>
        )}
      </Box>
    </Paper>
  );
}
