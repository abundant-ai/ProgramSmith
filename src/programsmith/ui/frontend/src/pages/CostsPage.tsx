import { useEffect, useState } from "react";
import { Clock3, Coins, RefreshCw, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type CostsResponse } from "../api";
import { Card, CardBody, CardHeader, CardTitle } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Skeleton } from "../components/ui/Skeleton";

const money = (value: number) => value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
const number = (value: number) => new Intl.NumberFormat(undefined, { notation: value >= 100_000 ? "compact" : "standard" }).format(value);
const duration = (ms: number) => ms ? `${(ms / 3_600_000).toFixed(1)}h` : "—";

export function CostsPage() {
  const [data, setData] = useState<CostsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    setLoading(true);
    api.costs().then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false));
  };
  useEffect(refresh, []);

  return (
    <div className="space-y-7">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">Costs</h1>
          <p className="mt-1 text-[13px] text-ink-3">Provider-reported model usage for local task generation and evaluation.</p>
        </div>
        <Button size="sm" variant="ghost" onClick={refresh}><RefreshCw className="size-3.5" />Refresh</Button>
      </div>

      {error && <div className="rounded-xl border border-danger/30 bg-danger-soft/20 px-4 py-2.5 text-sm text-danger">{error}</div>}
      {loading && !data ? <Skeleton className="h-40 w-full" /> : data && (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Metric icon={Coins} label="Model cost" value={money(data.totals.usd)} />
            <Metric icon={Sparkles} label="Sessions" value={number(data.totals.sessions)} />
            <Metric icon={Clock3} label="Agent time" value={duration(data.totals.duration_ms)} />
          </div>

          <Card>
            <CardHeader><CardTitle>By run</CardTitle></CardHeader>
            <CardBody className="p-0">
              {data.by_run.length === 0 ? (
                <p className="p-6 text-sm text-ink-3">No model sessions recorded yet. New runs appear here automatically.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full text-left text-[13px]">
                    <thead className="border-b border-line text-[11px] uppercase tracking-wider text-ink-4"><tr><th className="px-5 py-3">Run</th><th className="px-5 py-3">Cost</th><th className="px-5 py-3">Sessions</th><th className="px-5 py-3">Input</th><th className="px-5 py-3">Output</th><th className="px-5 py-3">Time</th></tr></thead>
                    <tbody className="divide-y divide-line">{data.by_run.map((row) => <tr key={row.run}><td className="px-5 py-3 font-medium text-ink"><Link className="hover:underline" to={`/run/${encodeURIComponent(row.run)}`}>{row.run}</Link></td><td className="px-5 py-3 font-mono text-ink-2">{money(row.usd)}</td><td className="px-5 py-3 text-ink-3">{row.sessions}</td><td className="px-5 py-3 text-ink-3">{number(row.input_tokens)}</td><td className="px-5 py-3 text-ink-3">{number(row.output_tokens)}</td><td className="px-5 py-3 text-ink-3">{duration(row.duration_ms)}</td></tr>)}</tbody>
                  </table>
                </div>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader><CardTitle>Recent sessions</CardTitle></CardHeader>
            <CardBody className="space-y-0 p-0">
              {data.recent.slice(0, 15).map((event) => (
                <div key={`${event.run}-${event.id}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-5 py-3 last:border-b-0">
                  <span className="w-40 truncate font-medium text-ink">{event.run}</span>
                  <span className="w-32 truncate font-mono text-[11.5px] text-ink-3">{event.stage}</span>
                  <span className="min-w-0 flex-1 truncate text-ink-4">{event.model || "model"}</span>
                  <span className="font-mono text-ink-2">{money(event.usd)}</span>
                </div>
              ))}
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
}

function Metric({ icon: Icon, label, value }: { icon: typeof Coins; label: string; value: string }) {
  return <Card><CardBody className="flex items-center gap-4"><div className="rounded-lg bg-surface-2 p-2.5"><Icon className="size-4 text-ink-3" /></div><div><p className="text-[11px] uppercase tracking-wider text-ink-4">{label}</p><p className="mt-0.5 text-lg font-semibold text-ink">{value}</p></div></CardBody></Card>;
}
