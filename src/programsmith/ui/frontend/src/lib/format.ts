/** Small presentation helpers. */

export function shortSha(sha: string | null | undefined, n = 10): string {
  if (!sha) return "—";
  return sha.length > n ? sha.slice(0, n) : sha;
}

export function titleCase(s: string): string {
  return s
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function relTime(ts: string | null | undefined): string {
  if (!ts) return "";
  const then = new Date(ts).getTime();
  if (Number.isNaN(then)) return ts;
  const diff = Date.now() - then;
  const s = Math.round(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function compactNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Best-effort numeric pass@1 in [0,1] from the string the API stores. */
export function parsePassAt1(v: string | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  const num = Number(v);
  if (!Number.isNaN(num) && num >= 0 && num <= 1) return num;
  return null;
}

export function fmtPassAt1(v: string | null | undefined): string {
  const n = parsePassAt1(v);
  if (n !== null) return `${Math.round(n * 100)}%`;
  return v ?? "—";
}
