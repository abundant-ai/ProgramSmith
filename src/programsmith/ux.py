"""Console UX for the hero commands: rules per stage, breadcrumb steps with timings, a Summary panel
and a Next-Steps panel at the end, a typed skip-vs-error taxonomy that names the remediation
flag/command in every message, cost preview + confirmation before anything bills, resume by default,
and SIGINT handling that cancels billable workers while preserving resumable trial plans.

Pure presentation + foreground drive loops. All pipeline decisions stay in the orchestrator/gates —
nothing here computes a verdict or reward; it only renders what the deterministic core recorded.
"""

from __future__ import annotations

import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

console = Console(highlight=False)

# Stage → human label (mirrors the dashboard's stage copy).
_STAGE_LABELS = {
    "INGEST_LOCK": "Ingest & lock",
    "TASK_MATRIX": "Task matrix",
    "ORACLE_GOLDEN": "Oracle & golden",
    "CREATE": "Create task",
    "SANITY": "Sanity gate",
    "STATIC_CI": "Static CI",
    "DIFFICULTY_SWEEP": "Smoke sweep",
    "CALIBRATE": "Calibrate",
    "QA_PROBE": "QA probe",
    "FULL_SWEEP": "Frontier sweep",
    "QA_GATE": "QA gate",
    "SYNTHESIZE": "Synthesize patch",
    "DONE": "Done",
    "DROPPED": "Dropped",
    "BLOCKED": "Blocked",
    "EASY_SHELF": "Easy shelf",
}


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage.replace("_", " ").title())


# ---- typed outcome taxonomy -----------------------------------------------------------------
# Every terminal/halt message carries a KIND and, when actionable, the exact flag/command that
# unblocks it (the SWE-gen lesson: an error without its remediation named is a support ticket).

@dataclass
class Outcome:
    kind: str        # done | easy | needs-review | waiting | skipped | blocked | error | paused
    icon: str
    headline: str
    advice: str = ""  # remediation with the flag/command NAMED; empty when nothing to do


def classify_halt(key: str, *, halted: str, stage: str, status: str, reason: str) -> Outcome:
    """Map a drive halt (+ the recorded reason) to a typed outcome. Substring matching over the
    orchestrator's honest halt reasons — presentation only, never a new decision."""
    r = (reason or "").lower()
    if halted == "draft":
        return Outcome("draft", "✓", f"{key}: Static CI passed",
                       f"out/drafts/{key} · not difficulty-calibrated")
    if halted == "terminal" or stage in ("DONE", "DROPPED", "BLOCKED", "EASY_SHELF"):
        if stage == "DONE" or status == "done":
            return Outcome("done", "✅", f"{key}: task accepted and exported", "")
        if stage == "EASY_SHELF" or status == "easy":
            return Outcome("easy", "🌤", f"{key}: frontier aced it — shelved as easy (out/easy/)",
                           "re-run with a stronger frontier, e.g. "
                           "--frontier anthropic/claude-opus-4-8, or keep it — easy tasks are "
                           "still valid, just not difficulty-calibrated")
        if stage == "DROPPED" or status == "dropped":
            if "copyleft" in r:
                return Outcome("skipped", "⏭", f"{key}: dropped — copyleft license",
                               "re-run with --allow-copyleft if your use permits it")
            if ("no viable task" in r or "proposed no candidates" in r
                    or ("candidate" in r and ("viable" in r or "recommended" in r or "selected" in r))):
                return Outcome("skipped", "⏭", f"{key}: dropped — {reason}",
                               f"the scope agent judged this repo unsuitable (a library, monorepo, or "
                               f"no deterministic CLI surface) — `programsmith status {key}` shows its "
                               f"rationale. Pick a single deterministic CLI tool; steer with --brief, "
                               f"or decide yourself with --review (then `programsmith pick {key} "
                               "--index N`) on a fresh run")
            return Outcome("skipped", "⏭", f"{key}: dropped — {reason}", "")
        return Outcome("blocked", "🔒", f"{key}: blocked — {reason}",
                       f"programsmith reopen {key} re-enters the harden loop with a fresh budget")
    if halted == "human":
        which = ("programsmith pick " + key + " --index N"
                 if stage == "TASK_MATRIX" else f"programsmith qa-gate {key} --decision accept")
        return Outcome("needs-review", "👤", f"{key}: waiting on your review at {stage_label(stage)}",
                       f"decide with `{which}` (or in the dashboard), then re-run to resume")
    if halted == "paused":
        return Outcome("paused", "⏸", f"{key}: operationally paused",
                       f"programsmith pause {key} --resume")
    # non-terminal halts: name the exact unblock for the known cases
    if "docker" in r and ("not" in r or "daemon" in r or "cannot connect" in r):
        return Outcome("error", "✗", f"{key}: Docker unavailable — {reason}",
                       "start Docker, verify with `programsmith doctor`, then re-run "
                       "(the run resumes from this stage)")
    if "credential" in r or "api key" in r or "api_key" in r:
        return Outcome("error", "✗", f"{key}: missing credentials — {reason}",
                       "set the named key in your env/.env (see `programsmith doctor`), then re-run")
    if "errored" in r and ("job" in r or "retry" in r or "attempts" in r):
        return Outcome("error", "✗", f"{key}: a background job errored out — {reason}",
                       f"fix the cause, then `programsmith retry {key}` clears the dead job and "
                       "the next pass relaunches it fresh")
    if "spend" in r or "authoriz" in r:
        return Outcome("blocked", "🔒", f"{key}: billable sweep not authorized — {reason}",
                       "re-run without --no-spend (the cost preview + confirm authorizes launches)")
    return Outcome("blocked", "🔒", f"{key}: parked at {stage_label(stage)} — {reason}",
                   f"programsmith status {key} shows the full history; re-running resumes from here")


# ---- cost preview + confirm -------------------------------------------------------------------

def _billable_rows(rc) -> list[tuple[str, str, str, int]]:
    """(stage, harness, model, trials) for every billable solver trial the run will launch.
    QA_PROBE rides the claude-code overlay (one auditor trial). oracle/nop baselines execute
    binaries, not LLMs — they never bill and are excluded."""
    rows: list[tuple[str, str, str, int]] = []
    for stage_name, stage in (("smoke", rc.difficulty), ("frontier", rc.full)):
        for a in (stage.agents or []):
            rows.append((stage_name, a.harness, a.model, a.n_trials))
    rows.append(("qa-probe", "claude-code", "(auditor)", 1))
    return rows


def cost_preview(rc, *, n_runs: int = 1, trial_cost_limit: float = 0.0) -> None:
    """What this will bill, BEFORE anything launches (per product policy: preview + confirm, no
    silent hard cap). Honest numbers only: trial counts are exact; the $ column is an upper bound
    and appears ONLY when a per-trial cap is configured — we never invent a price estimate."""
    rows = _billable_rows(rc)
    t = Table(title=f"Billable work per run{f'  ×{n_runs} runs' if n_runs > 1 else ''}",
              title_justify="left")
    t.add_column("stage"); t.add_column("harness"); t.add_column("model")
    t.add_column("trials", justify="right")
    if trial_cost_limit > 0:
        t.add_column("max $", justify="right")
    total = 0
    for stage_name, harness, model, n in rows:
        cells = [stage_name, harness, model, str(n)]
        if trial_cost_limit > 0:
            cells.append(f"{n * trial_cost_limit:.2f}")
        t.add_row(*cells)
        total += n
    console.print(t)
    per_run = f"{total} solver trials"
    if trial_cost_limit > 0:
        per_run += f", ≤ ${total * trial_cost_limit:.2f} (PROGRAMSMITH_TRIAL_COST_LIMIT cap)"
    else:
        per_run += (" — no per-trial $ cap set (mini-swe trials honor "
                    "PROGRAMSMITH_TRIAL_COST_LIMIT if you set one)")
    console.print(f"  per run: {per_run}, plus the pipeline's own agent cells "
                  f"(task matrix / oracle / create / QA — billed to your Anthropic credential)")


def confirm_or_abort(*, yes: bool, what: str) -> bool:
    """The spend gate: show-then-ask (--yes skips). Non-interactive stdin (CI) without --yes
    aborts safely rather than spending silently."""
    if yes:
        return True
    try:
        from rich.prompt import Prompt
        answer = Prompt.ask(
            f"Launch {what}? [dim][Y/n][/dim]",
            choices=["y", "n"],
            default="y",
            show_choices=False,
            show_default=False,
            console=console,
        )
        return answer.lower() == "y"
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]aborted (no confirmation; pass --yes to skip the prompt)[/dim]")
        return False


# ---- foreground drive loops --------------------------------------------------------------------

class _SigintOnce:
    """First Ctrl-C: cancel billable workers and park with completed trials intact. Re-running
    resumes only unfinished trials. Second Ctrl-C aborts after another defensive container stop."""

    def __init__(self):
        self.stop = False
        self._prev = None

    def __enter__(self):
        def handler(_sig, _frm):
            if self.stop:
                # The second interrupt still must not orphan billable Docker workers.
                from .local_runner import interrupt_active_solves
                interrupt_active_solves()
                raise KeyboardInterrupt
            self.stop = True
            console.print("\n[yellow]⏸ stopping active model trials, then parking — completed trials "
                          "stay saved; re-run the same command to resume unfinished work[/yellow]")
        self._prev = signal.signal(signal.SIGINT, handler)
        return self

    def __exit__(self, *a):
        signal.signal(signal.SIGINT, self._prev)
        return False


def _print_steps(steps: list[dict], last_stage: str | None) -> str | None:
    """A readable stage result, keeping raw FSM enum names and internal reasons out of hero UX."""
    for s in steps:
        if s["stage"] != last_stage:
            console.print()
            console.print(Rule(f"[bold]{stage_label(s['stage'])}[/bold]", align="left"))
            last_stage = s["stage"]
        icon = "✓" if s["verdict"] in ("pass", "done", "selected", "accept") else "•"
        if s["verdict"] == "selected":
            copy = "Candidate selected"
        elif s["verdict"] == "accept":
            copy = "Accepted"
        elif s["verdict"] in ("pass", "done"):
            copy = "Passed" if s["verdict"] == "pass" else "Complete"
        else:
            copy = s["verdict"].replace("_", " ").capitalize()
        destination = "" if s["verdict"] == "selected" else f"  [dim]Next: {stage_label(s['next'])}[/dim]"
        console.print(f"  [green]{icon}[/green] {copy}{destination}")
    return last_stage


def _run_activity(runs_dir: Path, key: str):
    """(active_job, waiting, summary) via the same read layer the dashboard uses — one definition
    of 'benignly in flight' everywhere."""
    from .ui.store import RunStore
    s = RunStore(runs_dir).summary(key)
    return s.active_job, s.waiting, s


def _stop_billable_workers() -> int:
    """Cancel queued local trials and synchronously stop named billable solver containers."""
    from .local_runner import interrupt_active_solves
    stopped = interrupt_active_solves()
    if stopped:
        console.print(f"  [yellow]stopped {stopped} active model trial(s)[/yellow]")
    return stopped


def drive_run_foreground(
    run_dir: Path,
    *,
    ctx: dict,
    interval: float = 5.0,
    drive_fn: Callable | None = None,
    notes_path: Path | None = None,
) -> Outcome:
    """One-shot foreground drive of a single run: repeat drive() passes, narrating stage Rules +
    step breadcrumbs, waiting (with a live clock) while a sweep/agent job is in flight, until the
    run parks at a terminal state, a human gate, or a genuine block. Returns the typed outcome."""
    from .orchestrator import drive
    drive_fn = drive_fn or drive
    run_dir = Path(run_dir)
    key = run_dir.name
    last_stage: str | None = None
    waited = 0.0
    with _SigintOnce() as sig:
        while True:
            if sig.stop:
                _stop_billable_workers()
                from .state import RunState
                stage = RunState.load(run_dir).current_stage.value
                return Outcome("paused", "⏸", f"{key}: parked at {stage_label(stage)} "
                               "(interrupted)", "re-run the same command to resume unfinished trials")
            from .state import RunState
            try:
                current = RunState.load(run_dir).current_stage.value
            except FileNotFoundError:  # injected/offline drive functions may not use persisted state
                current = last_stage or "pipeline"
            with console.status(f"[bold]{stage_label(current)}[/bold]  [dim]working…[/dim]"):
                res = drive_fn(run_dir, ctx=ctx, notes_path=notes_path)
            if res.steps:
                last_stage = _print_steps(res.steps, last_stage)
                waited = 0.0
            if res.halted in ("terminal", "human", "paused", "draft"):
                return classify_halt(key, halted=res.halted, stage=res.final_stage,
                                     status=res.final_status, reason=res.halt_reason)
            if sig.stop:
                _stop_billable_workers()
                return Outcome("paused", "⏸", f"{key}: parked at {stage_label(res.final_stage)} "
                               "(interrupted)", "re-run the same command to resume unfinished trials")
            active, waiting, _ = _run_activity(run_dir.parent, key)
            if active or waiting:
                waited += interval
                label = active or "sweep/analysis"
                with console.status(f"[dim]{stage_label(res.final_stage)}: {label} in flight "
                                    f"({waited:.0f}s)…[/dim]"):
                    time.sleep(interval)
                continue
            return classify_halt(key, halted=res.halted, stage=res.final_stage,
                                 status=res.final_status, reason=res.halt_reason)


def prune_docker() -> None:
    """Reclaim dangling image layers between batches (SWE-gen resource-hygiene pattern). Dangling
    ONLY — named task/overlay images stay cached for reuse. Never fatal."""
    try:
        p = subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True,
                           timeout=120)
        line = (p.stdout or "").strip().splitlines()
        if p.returncode == 0 and line:
            console.print(f"  [dim]docker image prune: {line[-1]}[/dim]")
    except Exception:  # noqa: BLE001 — hygiene must never kill a farm
        pass


def drive_fleet_foreground(
    runs_dir: Path,
    keys: list[str],
    *,
    ctx: dict,
    interval: float = 8.0,
    prune: bool = True,
    prune_every: int = 5,
    drive_fn: Callable | None = None,
    notes_path: Path | None = None,
) -> dict[str, Outcome]:
    """Foreground drive of a farm: round-robin drive() passes over the given runs until every one
    is parked (terminal / human gate / genuine block). Waiting runs (sweep in flight) stay in the
    rotation. Docker dangling-layer prunes run every `prune_every` completions."""
    from .orchestrator import drive
    drive_fn = drive_fn or drive
    runs_dir = Path(runs_dir)
    outcomes: dict[str, Outcome] = {}
    completed_since_prune = 0
    npass = 0
    with _SigintOnce() as sig:
        while True:
            npass += 1
            in_flight = 0
            for key in keys:
                if key in outcomes or sig.stop:
                    continue
                run_dir = runs_dir / key
                res = drive_fn(run_dir, ctx=ctx, notes_path=notes_path)
                if res.steps:
                    console.print(Rule(f"[bold]{key}[/bold]", align="left"))
                    _print_steps(res.steps, None)
                if res.halted in ("terminal", "human", "paused", "draft"):
                    outcomes[key] = classify_halt(key, halted=res.halted, stage=res.final_stage,
                                                  status=res.final_status, reason=res.halt_reason)
                    console.print(f"  {outcomes[key].icon} {outcomes[key].headline}")
                    completed_since_prune += 1
                    if prune and completed_since_prune >= prune_every:
                        prune_docker()
                        completed_since_prune = 0
                    continue
                active, waiting, _ = _run_activity(runs_dir, key)
                if active or waiting:
                    in_flight += 1
                    continue
                outcomes[key] = classify_halt(key, halted=res.halted, stage=res.final_stage,
                                              status=res.final_status, reason=res.halt_reason)
                console.print(f"  {outcomes[key].icon} {outcomes[key].headline}")
            if sig.stop:
                _stop_billable_workers()
                for key in keys:
                    outcomes.setdefault(key, Outcome(
                        "paused", "⏸", f"{key}: parked (interrupted)",
                        "re-run the same command to resume unfinished trials"))
            if len(outcomes) >= len(keys):
                if prune and completed_since_prune:
                    prune_docker()
                return outcomes
            with console.status(f"[dim]pass {npass}: {in_flight} run(s) in flight, "
                                f"{len(outcomes)}/{len(keys)} parked…[/dim]"):
                time.sleep(interval)


# ---- summary / next-steps panels ----------------------------------------------------------------

def summary_panel(outcomes: dict[str, Outcome]) -> None:
    by_kind: dict[str, int] = {}
    for o in outcomes.values():
        by_kind[o.kind] = by_kind.get(o.kind, 0) + 1
    lines = [f"[green]{o.icon}[/green] {o.headline}" for o in outcomes.values()]
    counts = "  ".join(f"{k}: {n}" for k, n in sorted(by_kind.items()))
    console.print()
    console.print(Panel("\n".join(lines) or "(nothing ran)", title="Complete",
                        subtitle=counts if len(outcomes) > 1 else None,
                        border_style="cyan", title_align="left", subtitle_align="left"))


def next_steps_panel(outcomes: dict[str, Outcome], *, runs_dir: Path,
                     dashboard_url: str | None = None) -> None:
    lines: list[str] = []
    for o in outcomes.values():
        if o.advice:
            lines.append(o.advice)
    if any(o.kind == "done" for o in outcomes.values()):
        lines.append("📦 accepted tasks are in out/tasks/ — point your Harbor/ProgramBench eval at them")
    if dashboard_url:
        lines.append(f"[bold cyan]Dashboard[/bold cyan]  [link={dashboard_url}]{dashboard_url}[/link]")
    else:
        lines.append(f"Dashboard  programsmith serve  [dim]({runs_dir})[/dim]")
    console.print(Panel("\n".join(dict.fromkeys(lines)), title="Output", border_style="dim",
                        title_align="left"))


def dashboard_panel(url: str, runs_dir: Path) -> None:
    """Prominent, clickable handoff printed as soon as the local dashboard is reachable."""
    console.print()
    console.print(Panel(
        f"[bold cyan][link={url}]{url}[/link][/bold cyan]\n[dim]Watching {runs_dir}[/dim]",
        title="Dashboard ready",
        border_style="cyan",
        title_align="left",
    ))


# ---- doctor -------------------------------------------------------------------------------------

def doctor_report(out: dict) -> int:
    """Render check_preflight() as the doctor table: required checks, then the per-provider
    credential table (which solver families are enabled). Exit code = readiness."""
    t = Table(title="programsmith doctor", title_justify="left")
    t.add_column(""); t.add_column("check"); t.add_column("detail")
    for c in out.get("checks", []):
        icon = "✅" if c.get("ok") else "❌"
        t.add_row(icon, c.get("name", "?"), c.get("detail", ""))
    console.print(t)
    providers = out.get("providers") or {}
    if providers:
        pt = Table(title="solver families (BYO keys — each provider is optional)",
                   title_justify="left")
        pt.add_column(""); pt.add_column("provider"); pt.add_column("credential")
        for name, info in providers.items():
            icon = "🔑" if info.get("present") else "—"
            pt.add_row(icon, name, info.get("detail", ""))
        console.print(pt)
    if out.get("ready"):
        console.print("[green]ready[/green]")
        return 0
    console.print("[red]not ready[/red] — fix the ❌ checks above, then re-run "
                  "`programsmith doctor`")
    return 1
