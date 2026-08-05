"""Small, provider-neutral local model-usage ledger.

Claude CLI result envelopes already contain token usage and the provider-reported USD amount. This
module records those facts without guessing prices, billing plans, or infrastructure costs.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

COSTS_FILE = "costs.jsonl"
_context: contextvars.ContextVar[tuple[Path, str] | None] = contextvars.ContextVar(
    "programsmith_cost_context", default=None
)
_lock = threading.RLock()


@contextlib.contextmanager
def cost_context(run_dir: str | Path, stage: str) -> Iterator[None]:
    token = _context.set((Path(run_dir), stage))
    try:
        yield
    finally:
        _context.reset(token)


def _num(d: dict, *names: str) -> int:
    for name in names:
        value = d.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _result_objects(text: str) -> list[dict]:
    objects: list[dict] = []
    stripped = (text or "").strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            objects.append(value)
    except (json.JSONDecodeError, TypeError):
        for line in stripped.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("type") == "result":
                objects.append(value)
    return objects


def record_envelope(text: str, *, model: str | None = None) -> None:
    """Record every result envelope in ``text`` under the active run/stage context."""
    current = _context.get()
    if current is None:
        return
    run_dir, stage = current
    for obj in _result_objects(text):
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        model_usage = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
        entries = [v for v in model_usage.values() if isinstance(v, dict)]
        input_tokens = _num(usage, "input_tokens", "inputTokens")
        output_tokens = _num(usage, "output_tokens", "outputTokens")
        cache_read = _num(usage, "cache_read_input_tokens", "cacheReadInputTokens")
        cache_create = _num(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
        if entries:
            input_tokens = sum(_num(v, "inputTokens", "input_tokens") for v in entries)
            output_tokens = sum(_num(v, "outputTokens", "output_tokens") for v in entries)
            cache_read = sum(_num(v, "cacheReadInputTokens", "cache_read_input_tokens") for v in entries)
            cache_create = sum(_num(v, "cacheCreationInputTokens", "cache_creation_input_tokens") for v in entries)
        usd_value = obj.get("total_cost_usd")
        usd = float(usd_value) if isinstance(usd_value, (int, float)) else sum(
            float(v.get("costUSD") or 0) for v in entries
        )
        session = str(obj.get("session_id") or obj.get("sessionId") or "")
        identity = session or hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:20]
        event = {
            "id": identity,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run": run_dir.name,
            "stage": stage,
            "model": model or next(iter(model_usage), None),
            "usd": usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_create,
            "duration_ms": _num(obj, "duration_ms", "durationMs"),
        }
        _append_unique(run_dir / COSTS_FILE, event)


def _append_unique(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if path.exists():
            for line in path.read_text(errors="replace").splitlines():
                try:
                    if json.loads(line).get("id") == event["id"]:
                        return
                except json.JSONDecodeError:
                    continue
        with path.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_events(runs_dir: str | Path) -> list[dict]:
    events: list[dict] = []
    root = Path(runs_dir)
    if not root.exists():
        return events
    for path in root.glob(f"*/{COSTS_FILE}"):
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue
    return sorted(events, key=lambda e: e.get("ts", ""), reverse=True)


def dashboard(runs_dir: str | Path) -> dict:
    # Opportunistically backfill sessions produced before the ledger was introduced. Agent logs are
    # append-only stream-json; session-id deduplication makes this safe on every refresh.
    root = Path(runs_dir)
    if root.exists():
        for log in root.glob("*/agent-logs/agent.log"):
            try:
                with cost_context(log.parents[1], "agent"):
                    record_envelope(log.read_text(errors="replace"))
            except OSError:
                continue
    events = read_events(runs_dir)

    def total(rows: list[dict]) -> dict:
        return {
            "usd": round(sum(float(r.get("usd") or 0) for r in rows), 6),
            "sessions": len(rows),
            "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
            "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
            "cache_read_tokens": sum(int(r.get("cache_read_tokens") or 0) for r in rows),
            "cache_creation_tokens": sum(int(r.get("cache_creation_tokens") or 0) for r in rows),
            "duration_ms": sum(int(r.get("duration_ms") or 0) for r in rows),
        }

    grouped: dict[str, list[dict]] = {}
    for event in events:
        grouped.setdefault(str(event.get("run") or "unknown"), []).append(event)
    by_run = [{"run": run, **total(rows)} for run, rows in grouped.items()]
    by_run.sort(key=lambda row: row["usd"], reverse=True)
    return {"totals": total(events), "by_run": by_run, "recent": events[:50]}
