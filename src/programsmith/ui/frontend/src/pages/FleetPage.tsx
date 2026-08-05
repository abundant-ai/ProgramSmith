import { useMemo, useState } from "react";
import InputAdornment from "@mui/material/InputAdornment";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";
import { Plus, Search } from "lucide-react";
import { api, type RunSummary } from "../api";
import { usePolling } from "../lib/usePolling";
import { RunCard } from "../components/RunCard";
import { NewRunModal } from "../components/NewRunModal";
import { Button } from "../components/ui/Button";
import { Skeleton } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";

function runCategory(run: RunSummary): string {
  if (run.screened_out) return "screened_out";
  if (!run.source_admitted) return "screening";
  if (run.paused) return "paused";
  if (run.awaiting_human) return "human";
  if (run.status === "draft") return "draft";
  if (run.status === "done") return "accepted";
  if (run.status === "easy") return "easy";
  if (run.status === "dropped") return "dropped";
  if (run.waiting) return "waiting";
  if (run.blocked || run.status === "blocked") return "blocked";
  return "in_progress";
}

const FILTERS: { key: string; label: string }[] = [
  { key: "tasks", label: "Tasks" },
  { key: "outputs", label: "Outputs" },
  { key: "all", label: "All" },
  { key: "in_progress", label: "In progress" },
  { key: "draft", label: "Drafts" },
  { key: "waiting", label: "Waiting" },
  { key: "human", label: "Needs review" },
  { key: "blocked", label: "Blocked" },
  { key: "accepted", label: "Exported" },
  { key: "easy", label: "Easy shelf" },
  { key: "screening", label: "Source screening" },
  { key: "screened_out", label: "Rejected sources" },
  { key: "dropped", label: "Dropped" },
  { key: "paused", label: "Paused" },
];

export function FleetPage() {
  const [modalOpen, setModalOpen] = useState(
    typeof window !== "undefined" && window.location.hash.startsWith("#new"),
  );
  const { data, error, initialLoading, refresh } = usePolling(() => api.listRuns(), 4000);
  const runs = data?.runs ?? [];
  const [filter, setFilter] = useState("tasks");
  const [query, setQuery] = useState("");

  const catCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const run of runs) {
      const key = runCategory(run);
      counts[key] = (counts[key] ?? 0) + 1;
    }
    return counts;
  }, [runs]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return runs.filter(
      (run) =>
        (filter === "all" ||
          (filter === "tasks"
            ? !!run.source_admitted
            : filter === "outputs"
              ? run.status === "draft" || run.status === "done"
              : runCategory(run) === filter)) &&
        (!q || (run.slug ?? "").toLowerCase().includes(q) || run.key.toLowerCase().includes(q)),
    );
  }, [runs, filter, query]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <Typography variant="h1">Runs</Typography>
        <Button variant="primary" onClick={() => setModalOpen(true)}>
          <Plus size={16} />
          New run
        </Button>
      </div>

      {error && !data ? (
        <ErrorState title="Couldn't load runs" message={error.message} action={<Button onClick={() => void refresh()}>Retry</Button>} />
      ) : initialLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-[150px] w-full" />)}
        </div>
      ) : runs.length === 0 ? (
        <EmptyState
          title="No runs"
          body="Create a run to source and verify a new task."
          action={<Button variant="primary" onClick={() => setModalOpen(true)}><Plus size={16} />New run</Button>}
        />
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <ToggleButtonGroup
              exclusive
              size="small"
              value={filter}
              onChange={(_, value: string | null) => value && setFilter(value)}
              aria-label="Run status"
              sx={{ flexWrap: "wrap", gap: "1px", "& .MuiToggleButtonGroup-grouped": { border: "1px solid", borderColor: "divider" } }}
            >
              {FILTERS.filter((item) => item.key === "tasks" || item.key === "outputs" || item.key === "all" || (catCounts[item.key] ?? 0) > 0).map((item) => (
                <ToggleButton key={item.key} value={item.key} sx={{ px: 1.25, py: 0.5, textTransform: "none", fontSize: 12 }}>
                  {item.label}&nbsp;{item.key === "all" ? runs.length : item.key === "tasks" ? (data?.counters.admitted ?? 0) : item.key === "outputs" ? (data?.counters.exported ?? 0) : catCounts[item.key] ?? 0}
                </ToggleButton>
              ))}
            </ToggleButtonGroup>
            <TextField
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search"
              size="small"
              sx={{ width: 190, ml: { sm: "auto" } }}
              slotProps={{ input: { startAdornment: <InputAdornment position="start"><Search size={15} /></InputAdornment> } }}
            />
          </div>

          {filtered.length === 0 ? (
            <div className="border border-line bg-surface px-4 py-8 text-center text-[13px] text-ink-3">No matching runs.</div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((run) => <RunCard key={run.key} run={run} />)}
            </div>
          )}
        </div>
      )}

      <NewRunModal open={modalOpen} onClose={() => setModalOpen(false)} onCreated={() => void refresh()} />
    </div>
  );
}
