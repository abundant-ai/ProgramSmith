"""The ProgramSmith CLI.

Hero surface (the README quick start):

    programsmith create --repo sharkdp/fd     # one repo → one calibrated task, foreground
    programsmith farm pb10-repos.txt          # many repos, driven to completion
    programsmith serve                        # dashboard + background auto-driver
    programsmith doctor                       # Docker / credentials / disk preflight
    programsmith status <key>                 # full run detail

Fleet ops: fleet, pick, qa-gate, retry, reopen, pause, presets, new (create without driving).
Per-stage plumbing (cells, gates, sweep imports) lives under `programsmith dev …`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from rich.panel import Panel

from .cells.create import assemble_skeleton
from .cells.oracle_golden import MINPACK_EPSILON, adopt_existing
from .cells.task_matrix import TaskMatrixOutput, apply_selection, propose
from .gates.ingest import ingest
from .gates.sanity import run_sanity, run_sanity_trials
from .gates.static_ci import run_static_ci
from .statestore import run_state_exists, store_for
from .trials import family_band, load_trials, pass_at_1
from .fsm import Stage
from .orchestrator import drive
from .manifest import Manifest, source_identity
from .state import RunState

# Forward DAG order + the sweep-key → stage map, used by `purge-synthetic` to find the earliest
# stage whose data was synthetic and roll the run back to it. QA_ON_GPT/PR are legacy drains
# (ADR-0039) — not part of the forward chain anymore, so they never appear here.
_FORWARD = [Stage.INGEST_LOCK, Stage.TASK_MATRIX, Stage.ORACLE_GOLDEN, Stage.CREATE, Stage.SANITY,
            Stage.STATIC_CI, Stage.DIFFICULTY_SWEEP, Stage.CALIBRATE, Stage.QA_PROBE,
            Stage.FULL_SWEEP, Stage.QA_GATE]
_SWEEP_STAGE = {"sanity": Stage.SANITY, "difficulty": Stage.DIFFICULTY_SWEEP,
                "qa_probe": Stage.QA_PROBE, "full": Stage.FULL_SWEEP}

TASK_MATRIX_FILE = "task_matrix.json"


def _cmd_ingest(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    identity = source_identity(args.repo, args.sha)
    run_id = args.run_id or f"run-{identity}"

    manifest = Manifest(run_id=run_id, task_identity=identity, slug=args.slug)
    state = RunState.start(run_id, identity, slug=args.slug, ts=args.ts)

    result = ingest(
        manifest, args.repo, args.sha, work_dir=run_dir,
        repo_url=args.url, local_path=Path(args.local) if args.local else None,
        block_copyleft=not args.allow_copyleft,
    )
    state.advance(result.verdict, ts=args.ts)
    manifest.save(run_dir)
    state.save(run_dir)

    print(f"[INGEST] {result.verdict.upper()} — {result.reason}")
    print(json.dumps(result.detail, indent=2))
    print(f"[FSM] now at {state.current_stage.value} (status={state.status})")
    return 0 if result.verdict == "pass" else 1


def _cmd_task_matrix(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)
    if state.current_stage.value != "TASK_MATRIX":
        print(f"[TASK MATRIX] run is at {state.current_stage.value}, not TASK_MATRIX; aborting.")
        return 2

    from .config import LhConfig
    # TASK MATRIX is a light one-shot (ADR-0042 routing) — cell_model_light unless overridden.
    out: TaskMatrixOutput = propose(manifest, model=args.model or LhConfig.load().cell_model_light)
    (run_dir / TASK_MATRIX_FILE).write_text(out.model_dump_json(indent=2) + "\n")

    print(f"[TASK MATRIX] {len(out.candidates)} candidate(s) for {out.source_ref} — HUMAN REVIEW #1:")
    for i, c in enumerate(out.candidates):
        print(f"  [{i}] {c.tool_name} (bin {c.binary_name}) · {c.upstream_language} · "
              f"~{c.est_kloc} kLOC · diff={c.expected_difficulty} · {c.expert_hours}h · "
              f"{c.recommendation}\n      scope: {c.flag_surface}\n"
              f"      basis: {c.basis_ref}\n      why: {c.rationale}")
    print(f"\nSelect with:  programsmith select --run-dir {args.run_dir} --pick <index>   (or --none)")
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)
    if args.none:
        state.advance("none_selected", ts=args.ts)
        state.save(run_dir)
        print(f"[SELECT] no candidate chosen → {state.current_stage.value} ({state.status})")
        return 0

    out = TaskMatrixOutput.model_validate_json((run_dir / TASK_MATRIX_FILE).read_text())
    c = out.candidates[args.pick]
    # apply_selection is the ONE code path (shared with the UI pick + the auto-pick handler) that
    # sets the ProgramBench dimensions and recomputes the dedup identity (ADR-0038 axes).
    apply_selection(manifest, c)
    state.task_identity = manifest.task_identity
    state.advance("selected", ts=args.ts)
    manifest.save(run_dir)
    state.save(run_dir)
    print(f"[SELECT] chose [{args.pick}] {c.tool_name} ({c.flag_surface}) → identity "
          f"{manifest.task_identity}; now at {state.current_stage.value}")
    return 0


def _cmd_oracle_golden(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)
    if args.generate:
        # Agentic clean-room generate-mode (needs an execution env). For sources WITHOUT a pre-built
        # reference bundle; generating minpack's 8h reference port stays out of scope (ADR-0016).
        from .cells.oracle_golden import generate
        print(f"[ORACLE+GOLDEN/generate] driving clean-room port + golden capture into {args.bundle}…")
        out, res, _agent = generate(manifest, Path(args.bundle), max_iters=args.max_iters, model=args.model)
    else:
        # Dogfood uses the minpack epsilon (from parity.rs). General runs propose epsilon in-cell.
        out, res = adopt_existing(
            manifest, Path(args.bundle), MINPACK_EPSILON,
            epsilon_justification="minpack tolerances from _build/minpack-crate/oracle/tests/parity.rs",
            capture_method="cminpack double-precision golden generator (_gen/gen_goldens.c, fp-contract=off)",
        )
    (run_dir / "oracle_golden.json").write_text(out.model_dump_json(indent=2) + "\n")
    print(f"[ORACLE+GOLDEN] {res.verdict.upper()} — {res.reason}")
    if state.current_stage.value == "ORACLE_GOLDEN":
        state.advance(res.verdict, ts=args.ts)
        manifest.save(run_dir)
        state.save(run_dir)
        print(f"[FSM] now at {state.current_stage.value} (status={state.status})")
    return 0 if res.verdict == "pass" else 1


def _cmd_synthesize(args: argparse.Namespace) -> int:
    """SYNTHESIZE/REVISE — produce a validated patch plan (LLM, quarantined) and, with --apply, drive
    the agentic apply loop (needs an execution env). The FSM then re-runs STATIC_CI on the patch."""
    from .cells.synthesize import apply as synth_apply, synthesize_plan
    run_dir = Path(args.run_dir)
    manifest = Manifest.load(run_dir)
    task_dir = Path(args.task_dir) if args.task_dir else run_dir / "task" / (manifest.slug or "rewrite-task")
    findings = args.finding or None
    plan = synthesize_plan(str(task_dir), args.move, args.from_stage, args.reason,
                           findings=findings, model=args.model)
    (run_dir / "synthesize.json").write_text(plan.model_dump_json(indent=2) + "\n")
    print(f"[SYNTHESIZE] {args.move} plan: {len(plan.patch)} edit(s); preserves_identity="
          f"{plan.preserves_identity}")
    for e in plan.patch:
        print(f"    edit {e.file}: {e.change}")
    if not args.apply:
        print("\nApply is gated: re-run with --apply on an execution-capable host to edit + re-validate.")
        return 0
    res = synth_apply(plan, str(task_dir), max_iters=args.max_iters, model=args.model)
    print(f"[SYNTHESIZE/apply] {'OK' if res.success else 'INCOMPLETE'} — {res.reason}")
    return 0 if res.success else 1


def _cmd_create(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)
    task_dir = Path(args.out) if args.out else run_dir / "task" / (manifest.slug or "rewrite-task")
    out = assemble_skeleton(manifest, task_dir)
    (run_dir / "create.json").write_text(out.model_dump_json(indent=2) + "\n")
    print(f"[CREATE] assembled hybrid skeleton at {task_dir} ({len(out.files)} files, "
          f"{len(out.todos)} TODO fill-points; pack {out.template_pack_version})")
    for t in out.todos:
        print(f"    TODO {t}")
    if args.fill:
        # Agentic fill: needs an execution env (local Docker, ADR-0019).
        from .cells.create import agentic_fill
        print(f"[CREATE/fill] driving the agentic fill loop (≤{args.max_iters} iterations)…")
        res = agentic_fill(manifest, task_dir, max_iters=args.max_iters, model=args.model)
        print(f"[CREATE/fill] {'OK' if res.success else 'INCOMPLETE'} — {res.reason}")
        if not res.success:
            return 1
    if state.current_stage.value == "CREATE":
        state.advance("pass", ts=args.ts)
        state.save(run_dir)
        print(f"[FSM] now at {state.current_stage.value} (status={state.status})")
    return 0


def _cmd_sanity(args: argparse.Namespace) -> int:
    res = run_sanity(Path(args.task_dir), image_tag=args.tag, build=not args.no_build)
    print(f"[SANITY] {res.verdict.upper()} — {res.reason}")
    for name, ok in res.detail.get("checks", {}).items():
        print(f"  {'✓' if ok else '✗'} {name}")
    if args.run_dir and args.advance:
        state = RunState.load(args.run_dir)
        if state.current_stage.value == "SANITY":
            state.advance(res.verdict, ts=args.ts)
            state.save(args.run_dir)
            print(f"[FSM] now at {state.current_stage.value} (status={state.status})")
    return 0 if res.verdict == "pass" else 1


def _cmd_static_ci(args: argparse.Namespace) -> int:
    res = run_static_ci(Path(args.repo_root), args.task)
    print(f"[STATIC CI] {res.verdict.upper()} — {res.reason}")
    for name, r in res.detail["results"].items():
        mark = "✓" if r["status"] == "pass" else "✗"
        print(f"  {mark} {name} ({r['status']})" + (f"  rc={r['rc']}" if r["status"] != "pass" else ""))
    if args.run_dir and args.advance:
        state = RunState.load(args.run_dir)
        if state.current_stage.value == "STATIC_CI":
            state.advance(res.verdict, ts=args.ts)
            state.save(args.run_dir)
            print(f"[FSM] now at {state.current_stage.value} (status={state.status})")
    return 0 if res.verdict == "pass" else 1


def _cmd_sweep_read(args: argparse.Namespace) -> int:
    """Import externally-produced trials: ingest a directory of trial artifacts (a local sweep's
    experiment dir, or any exported trial tree) or a status-payload JSON, compute the band/verdict
    for the given kind, record it into manifest.sweeps[kind], and (optionally) advance the FSM."""
    run_dir = Path(run_dir := Path(args.run_dir))
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)
    source = args.from_pull or args.status_json
    if not source:
        print("[SWEEP READ] supply --from-pull <dir> or --status-json <file>")
        return 2
    trials = load_trials(source)
    if not trials and args.kind != "qa_probe":
        # qa_probe reads the auditor's JSON report from the trajectory text, not the trial records,
        # so an empty trial parse must not dead-end it.
        print(f"[SWEEP READ] no trials found in {source!r}")
        return 1

    if args.kind == "sanity":
        res = run_sanity_trials(trials)
        manifest.sweeps["sanity"] = {
            "source": "baseline-trials", "experiment": args.experiment,
            "trials": [t for t in trials if t.get("agent") in ("oracle", "nop")],
            "verdict": res.verdict, **res.detail,
        }
        verdict, stage_name, msg = res.verdict, "SANITY", res.reason
    elif args.kind == "difficulty":
        pa = pass_at_1(trials)
        manifest.sweeps["difficulty"] = {
            "experiment": args.experiment, "pass_at_1": pa["aggregate"], "groups": pa["groups"],
        }
        verdict, stage_name = "done", "DIFFICULTY_SWEEP"
        msg = f"aggregate pass@1={pa['aggregate']}"
    elif args.kind == "qa_probe":
        # The Task Construction Auditor's verdict lives in the pulled trajectory text (its JSON
        # report), not in the trial records — same read the auto-driver does at QA_PROBE. The
        # verdict is the agent's own output; when no JSON verdict parses we record
        # `complete_unparsed` and STOP (a verdict is never invented — invariant #4).
        from .trials import extract_auditor_verdict
        from .probes import GAMEABLE_VERDICTS
        info = extract_auditor_verdict(source)
        if not info["found"]:
            manifest.sweeps["qa_probe"] = {"status": "complete_unparsed",
                                           "experiment": args.experiment, "pull_dir": str(source)}
            manifest.save(run_dir)
            print("[SWEEP READ] kind=qa_probe → no auditor JSON verdict parsed — review the "
                  "trajectory (verdict not invented)")
            return 1
        verdict = "harden" if (info["verdict"] in GAMEABLE_VERDICTS or info["blockers"] > 0) else "clean"
        manifest.sweeps["qa_probe"] = {
            "status": "done", "experiment": args.experiment, "pull_dir": str(source),
            "verdict": verdict, "auditor_verdict": info["verdict"],
            "blocker_findings": info["blockers"],
            "summary": f"auditor: {info['verdict']}, {info['blockers']} blocker finding(s)",
        }
        stage_name = "QA_PROBE"
        msg = f"auditor {info['verdict']}, {info['blockers']} blocker(s) → {verdict}"
    else:  # full — record the GENERIC band (matches the orchestrator's finalize): the N-family
        # map + max pairwise fairness gap, driven off pass_at_1 so any configured harness
        # (mini-swe/gemini-cli/…) measures. Legacy cc/cx fields ride along for old readers.
        pa = pass_at_1(trials)
        band = family_band(trials)
        from .runconfig import band_verdict, effective_run_config
        bv = band_verdict(pa["groups"], effective_run_config(manifest).full.band)
        manifest.sweeps["full"] = {"experiment": args.experiment, "status": "done",
                                   "pass_at_1": pa["aggregate"], "groups": pa["groups"],
                                   "band_verdict": bv,
                                   "families": band["families"],
                                   "claude_code": band["families"].get("claude-code"),
                                   "codex": band["families"].get("codex"),
                                   "fairness_gap": band["fairness_gap"]}
        verdict, stage_name = "done", "FULL_SWEEP"
        msg = f"aggregate={pa['aggregate']} band_verdict={bv} groups={list((pa['groups'] or {}).keys())}"

    manifest.save(run_dir)
    print(f"[SWEEP READ] kind={args.kind} → {msg}")
    print(f"  recorded {len(trials)} trial(s) into manifest.sweeps[{args.kind!r}]")
    if args.advance and state.current_stage.value == stage_name:
        state.advance(verdict, ts=args.ts)
        state.save(run_dir)
        print(f"[FSM] now at {state.current_stage.value} (status={state.status})")
    return 0 if verdict in ("pass", "done", "clean") else 1


def _cmd_run(args: argparse.Namespace) -> int:
    ctx = {k: v for k, v in {"oracle_bundle": args.oracle_bundle,
                             "ci_repo_root": args.ci_repo_root,
                             "task_path": args.task_path}.items() if v}
    if args.spend:
        ctx["sweep_live"] = True   # authorize real sweeps to launch (bills the operator's own key)
    res = drive(args.run_dir, ctx=ctx, max_steps=args.max_steps,
                notes_path=Path(args.run_dir).parent / "WORKFLOW_NOTES.md")
    for s in res.steps:
        print(f"  ✓ {s['stage']} --{s['verdict']}--> {s['next']}  ({s['reason']})")
    if not res.steps:
        print("  (no stages advanced)")
    icon = {"human": "👤", "paused": "⏸", "terminal": "■", "blocked": "🔒", "max_steps": "…"}.get(res.halted, "•")
    print(f"\n{icon} HALTED [{res.halted}] at {res.final_stage} (status={res.final_status})\n   {res.halt_reason}")
    return 0


def _cmd_autodrive(args: argparse.Namespace) -> int:
    """Auto-driver: advance the whole fleet hands-free, one drive() pass per interval. Halts each run
    at its next human gate / pause / un-runnable stage. Never synthesizes; with --spend it launches
    the billable sweeps for real (experiment recorded). Ctrl-C to stop."""
    from .daemon import autodrive_loop, autodrive_once
    ctx = {k: v for k, v in {"ci_repo_root": args.ci_repo_root}.items() if v}
    if args.spend:
        ctx["sweep_live"] = True
    notes = Path(args.runs_dir).parent / "WORKFLOW_NOTES.md"
    print(f"[autodrive] {'SPEND authorized — real sweeps will launch on your key' if args.spend else 'no-spend — billable stages halt blocked'}")

    def _report(n: int, records: list[dict]) -> None:
        moved = [r for r in records if r["advanced"]]
        if moved or args.verbose:
            print(f"[autodrive pass {n}] {len(records)} run(s) checked, {len(moved)} advanced:")
            for r in (moved or records):
                print(f"    {r['key']:14} +{r['advanced']} → {r['final_stage']} "
                      f"[{r['halted']}] {r['halt_reason'][:70]}")

    if args.once:
        _report(1, autodrive_once(args.runs_dir, ctx=ctx, notes_path=notes))
        return 0
    print(f"[autodrive] watching {args.runs_dir} every {args.interval}s (Ctrl-C to stop)")
    try:
        autodrive_loop(args.runs_dir, interval=args.interval, ctx=ctx,
                       notes_path=notes, max_passes=args.max_passes, on_pass=_report)
    except KeyboardInterrupt:
        print("\n[autodrive] stopped")
    return 0


def _dashboard_state_path() -> Path:
    """Per-install dashboard state, beside the local ProgramSmith config.

    Keeping this out of the repository's run tree lets ``programsmith stop`` work from any
    directory, including after the shell that launched the dashboard has exited.
    """
    if override := os.getenv("PROGRAMSMITH_DASHBOARD_STATE"):
        return Path(override).expanduser()
    if config_override := os.getenv("PROGRAMSMITH_CONFIG_PATH"):
        return Path(config_override).expanduser().parent / "dashboard.json"
    return Path.home() / ".programsmith" / "dashboard.json"


def _dashboard_log_path() -> Path:
    return _dashboard_state_path().with_name("dashboard.log")


def _load_dashboard_state() -> dict | None:
    path = _dashboard_state_path()
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _save_dashboard_state(payload: dict) -> None:
    path = _dashboard_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    tmp.replace(path)
    path.chmod(0o600)


def _clear_dashboard_state() -> None:
    try:
        _dashboard_state_path().unlink()
    except FileNotFoundError:
        pass


def _configure_dashboard_environment(args: argparse.Namespace, runs_dir: Path) -> None:
    """Bind the server and auto-driver to one fleet."""
    os.environ["PROGRAMSMITH_RUNS_DIR"] = str(runs_dir)
    if args.autodrive:
        os.environ["PROGRAMSMITH_AUTODRIVE"] = "1"
        os.environ["PROGRAMSMITH_AUTODRIVE_INTERVAL"] = str(args.autodrive_interval)
        os.environ["PROGRAMSMITH_AUTODRIVE_SPEND"] = "1" if args.spend else "0"
        if args.ci_repo_root:
            os.environ["PROGRAMSMITH_CI_REPO_ROOT"] = args.ci_repo_root
    else:
        os.environ.pop("PROGRAMSMITH_AUTODRIVE", None)
        os.environ["PROGRAMSMITH_AUTODRIVE_SPEND"] = "0"


def _print_dashboard_panel(info: dict, *, foreground: bool = False) -> None:
    from . import ux

    url = info["url"]
    driver = (f"On · every {float(info.get('autodrive_interval', 8)):g}s"
              if info.get("autodrive") else "Off · view only")
    lifecycle = "Foreground" if foreground else ("Already running" if info.get("reused") else "Running in background")
    lines = (
        f"[bold cyan][link={url}]{url}[/link][/bold cyan]\n\n"
        f"[bold]Status[/bold]      {lifecycle}\n"
        f"[bold]Runs[/bold]        {info['runs_dir']}\n"
        f"[bold]Autodrive[/bold]   {driver}\n"
        f"[bold]Sweeps[/bold]      {'On' if info.get('spend') else 'Off'}"
    )
    if not foreground:
        lines += f"\n[bold]Stop[/bold]        programsmith stop\n[bold]Logs[/bold]        {info['log_path']}"
    ux.console.print()
    ux.console.print(Panel(
        lines,
        title="ProgramSmith dashboard",
        border_style="cyan",
        title_align="left",
    ))


def _start_dashboard(
    runs_dir: Path,
    host: str,
    port: int,
    *,
    autodrive: bool,
    autodrive_interval: float,
    spend: bool,
    ci_repo_root: str | None,
) -> dict | None:
    """Start a detached dashboard, or reuse the matching healthy instance.

    The child starts a new process session and writes only to an owner-local log. The short-lived
    CLI waits for the health endpoint, records the verified PID + instance token, and returns. This
    is what makes Ctrl-C in the caller's shell irrelevant after ``serve`` has returned.
    """
    import secrets
    import subprocess
    import sys
    import time

    expected_runs = str(Path(runs_dir).expanduser().resolve())
    chosen = None
    for candidate in range(port, port + 20):
        health = _dashboard_health(host, candidate)
        if health and health.get("runs_dir") == expected_runs:
            info = {
                "url": f"http://{host}:{candidate}",
                "host": host,
                "port": candidate,
                "pid": health.get("pid"),
                "token": health.get("instance_id"),
                "runs_dir": expected_runs,
                "autodrive": bool(health.get("autodrive")),
                "autodrive_interval": float(health.get("autodrive_interval", 8)),
                "spend": bool(health.get("spend")),
                "log_path": str(_dashboard_log_path()),
                "reused": True,
            }
            if info["pid"] and info["token"]:
                _save_dashboard_state(info)
            return info
        if not _port_is_open(host, candidate):
            chosen = candidate
            break
    if chosen is None:
        return None

    port = chosen
    token = secrets.token_urlsafe(18)
    log_path = _dashboard_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, "-m", "programsmith", "serve", "--foreground",
        "--runs-dir", str(runs_dir), "--host", host, "--port", str(port),
        "--autodrive" if autodrive else "--no-autodrive",
        "--spend" if spend else "--no-spend",
        "--autodrive-interval", str(autodrive_interval),
    ]
    if ci_repo_root:
        argv.extend(["--ci-repo-root", ci_repo_root])
    env = os.environ.copy()
    env["PROGRAMSMITH_DASHBOARD_TOKEN"] = token
    try:
        with log_path.open("ab", buffering=0) as log:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        log_path.chmod(0o600)
    except Exception:  # noqa: BLE001 — missing UI dependency / unwritable local state
        return None

    info = {
        "url": f"http://{host}:{port}",
        "host": host,
        "port": port,
        "pid": proc.pid,
        "token": token,
        "runs_dir": expected_runs,
        "autodrive": autodrive,
        "autodrive_interval": autodrive_interval,
        "spend": spend,
        "log_path": str(log_path),
        "reused": False,
    }
    _save_dashboard_state(info)  # provisional: Ctrl-C during startup still leaves a stoppable child
    for _ in range(40):  # ~10s for Uvicorn + static mount to bind
        if proc.poll() is not None:
            _clear_dashboard_state()
            return None
        health = _dashboard_health(host, port)
        if (health and health.get("runs_dir") == expected_runs
                and health.get("pid") == proc.pid and health.get("instance_id") == token):
            return info
        time.sleep(0.25)

    proc.terminate()
    _clear_dashboard_state()
    return None


def _cmd_serve(args: argparse.Namespace) -> int:
    if not _is_loopback_host(args.host):
        from . import ux
        ux.console.print(
            "[red]Refusing to expose the unauthenticated local dashboard on "
            f"{args.host!r}.[/red]\nUse the default 127.0.0.1 binding and an SSH tunnel if you "
            "need remote access."
        )
        return 2
    runs_dir = _remember_runs_dir(args)
    if not args.foreground:
        info = _start_dashboard(
            runs_dir, args.host, args.port,
            autodrive=args.autodrive,
            autodrive_interval=args.autodrive_interval,
            spend=args.spend,
            ci_repo_root=args.ci_repo_root,
        )
        if not info:
            from . import ux
            ux.console.print("[red]Dashboard failed to start.[/red] See "
                             f"{_dashboard_log_path()} for details.")
            return 1
        _print_dashboard_panel(info)
        return 0

    import uvicorn  # noqa: PLC0415 — optional [ui] dep

    _configure_dashboard_environment(args, runs_dir)
    url = f"http://{args.host}:{args.port}"
    _print_dashboard_panel({
        "url": url,
        "runs_dir": str(runs_dir),
        "autodrive": args.autodrive,
        "autodrive_interval": args.autodrive_interval,
        "spend": args.spend,
        "log_path": str(_dashboard_log_path()),
    }, foreground=True)
    uvicorn.run("programsmith.ui.app:app", host=args.host, port=args.port, reload=False, log_level="warning")
    return 0


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_loopback_host(host: str) -> bool:
    """The local dashboard has no remote-user auth and must never bind publicly."""
    import ipaddress

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _cmd_stop(_args: argparse.Namespace) -> int:
    """Stop the detached dashboard recorded for this ProgramSmith installation."""
    import signal
    import time

    from . import ux

    state = _load_dashboard_state()
    if not state:
        ux.console.print("[dim]Dashboard is not running.[/dim]")
        return 0
    try:
        pid = int(state["pid"])
        port = int(state["port"])
        host = str(state["host"])
    except (KeyError, TypeError, ValueError):
        _clear_dashboard_state()
        ux.console.print("[dim]Removed stale dashboard state; no process was stopped.[/dim]")
        return 0

    health = _dashboard_health(host, port)
    if health and (health.get("pid") != pid or health.get("instance_id") != state.get("token")):
        _clear_dashboard_state()
        ux.console.print("[yellow]Dashboard state was stale; the process on that port was left untouched.[/yellow]")
        return 1
    if pid == os.getpid():
        ux.console.print("[red]Refusing to stop the current CLI process.[/red]")
        return 1
    if not _pid_is_alive(pid):
        _clear_dashboard_state()
        ux.console.print("[dim]Dashboard was already stopped.[/dim]")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(50):
        if not _pid_is_alive(pid):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    _clear_dashboard_state()
    ux.console.print(Panel(
        f"[bold green]Stopped[/bold green]  {state.get('url', f'http://{host}:{port}')}",
        title="ProgramSmith dashboard",
        border_style="green",
        title_align="left",
    ))
    return 0


def _port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if something already accepts TCP connections at host:port (a dashboard likely up)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dashboard_health(host: str, port: int, timeout: float = 0.75) -> dict | None:
    """Return ProgramSmith's health payload, never mistaking an unrelated open port for the UI."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        return payload if payload.get("ok") is True and "frontend_built" in payload else None
    except Exception:  # noqa: BLE001 — closed port, non-ProgramSmith server, or malformed response
        return None


def _ensure_dashboard(runs_dir: Path, host: str, port: int, *, run_key: str | None = None) -> str | None:
    """Auto-start the local dashboard in VIEW-ONLY mode (no auto-driver — the foreground command is
    the driver; a second driver on the same runs-dir would double-drive) and return a link, deep to
    `run_key` when given. Idempotent: if the port is already serving, just returns the link. Best-
    effort — a dashboard that won't come up must never block the actual run, so failures return None.
    The server is spawned DETACHED (its own session) so it outlives the foreground command and the
    user can keep inspecting after the run parks."""
    info = _start_dashboard(
        runs_dir, host, port,
        autodrive=False,
        autodrive_interval=8.0,
        spend=False,
        ci_repo_root=None,
    )
    if not info:
        return None
    base = info["url"]
    return f"{base}/run/{run_key}" if run_key else base


def _open_dashboard(url: str) -> bool:
    """Open a local dashboard without ever making task creation depend on desktop integration."""
    if os.getenv("CI") or os.getenv("PROGRAMSMITH_NO_BROWSER"):
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 — missing browser/headless sessions are expected
        return False


def _cmd_purge_synthetic(args: argparse.Namespace) -> int:
    """Strip ALL synthetic data from a run and roll the FSM back to its last REAL stage. Removes any
    manifest oracle/sweep entry tagged `simulated`, deletes a synthetic-built task skeleton when the
    rollback reaches CREATE-or-earlier, and rebuilds the state by replaying only the real-verdict
    history prefix. The run then resumes from there on the REAL path."""
    import shutil
    run_dir = Path(args.run_dir)
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)

    sweeps = manifest.sweeps or {}
    removed: list[str] = []
    synthetic_stages: list[Stage] = []
    for k in list(sweeps):
        v = sweeps[k]
        if isinstance(v, dict) and v.get("simulated"):
            synthetic_stages.append(_SWEEP_STAGE.get(k))
            removed.append(f"sweep:{k}")
            del sweeps[k]
    if isinstance(manifest.oracle, dict) and manifest.oracle.get("simulated"):
        synthetic_stages.append(Stage.ORACLE_GOLDEN)
        removed.append("oracle")
        manifest.oracle = None
    if isinstance(manifest.snapshot, dict) and (manifest.snapshot.get("pr") or {}).get("simulated"):
        manifest.snapshot = {kk: vv for kk, vv in manifest.snapshot.items() if kk != "pr"}
        removed.append("snapshot.pr")

    synthetic_stages = [s for s in synthetic_stages if s is not None]
    if not removed:
        print(f"[purge] {run_dir.name}: no synthetic data found; nothing to roll back "
              f"(at {state.current_stage.value})")
        return 0

    target = min(synthetic_stages, key=_FORWARD.index) if synthetic_stages else state.current_stage
    kept = []
    for ev in state.history:
        if ev.stage == target:
            break
        kept.append(ev)
    # a task skeleton built during the synthetic run is invalid → drop it so CREATE re-runs for real
    if _FORWARD.index(target) <= _FORWARD.index(Stage.CREATE):
        for rel in ("task", "create.json", "oracle_golden.json"):
            p = run_dir / rel
            if p.is_dir():
                shutil.rmtree(p); removed.append(f"{rel}/")
            elif p.exists():
                p.unlink(); removed.append(rel)
    _store, _key = store_for(run_dir)
    _store.delete(f"{_key}/drive.json")  # stale trace referencing the purged stages (idempotent)

    new_state = RunState.start(state.run_id, state.task_identity, slug=state.slug, ts=state.created_at)
    for ev in kept:
        new_state.advance(ev.verdict)
    manifest.save(run_dir)
    new_state.save(run_dir)
    print(f"[purge] {run_dir.name}: removed {removed}")
    print(f"[purge] rolled back {state.current_stage.value} → {new_state.current_stage.value} "
          f"(last REAL stage); resume on the real path")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    state = RunState.load(args.run_dir)
    print(f"run_id={state.run_id} stage={state.current_stage.value} status={state.status} "
          f"harden={state.harden} revise={state.revise}")
    for ev in state.history:
        print(f"  {ev.stage.value} --{ev.verdict}--> {ev.next.value}  ({ev.reason})")
    return 0


# ===== fleet-level CLI: everything the UI does, batchable + scriptable (key-based) ====
# These resolve a run by KEY against the configured runs-dir (the same dir `programsmith serve` drives), so the
# whole fleet is operable headlessly: farm, check status, advance, gate, browse files, probe.

def _runs_dir(args: argparse.Namespace) -> Path:
    from .config import LhConfig
    return Path(getattr(args, "runs_dir", None) or LhConfig.load().runs_dir)


def _remember_runs_dir(args: argparse.Namespace) -> Path:
    """Persist an explicitly selected fleet so a later bare ``programsmith serve`` finds it."""
    runs_dir = _runs_dir(args).expanduser().resolve()
    if getattr(args, "runs_dir", None):
        from .config import LhConfig
        cfg = LhConfig.load_persisted()
        cfg.runs_dir = str(runs_dir)
        cfg.save()
    return runs_dir


def _run_dir_for(args: argparse.Namespace) -> Path:
    return _runs_dir(args) / args.key


def _print_table(headers: list[str], rows: list[tuple]) -> None:
    cols = list(zip(*([tuple(headers)] + rows))) if rows else [(h,) for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def _git_head(url: str) -> str:
    import subprocess
    try:
        p = subprocess.run(["git", "ls-remote", url, "HEAD"], capture_output=True, text=True,
                           timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"could not resolve HEAD for {url}: git ls-remote timed out after 60s "
                           "(network/GitHub hiccup — retry, or pass --sha to skip the lookup)")
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"could not resolve HEAD for {url}: {p.stderr.strip()[:200]}")
    return p.stdout.split()[0]


def _is_dropped(run_dir: Path) -> bool:
    """True if the run has already reached a terminal DROPPED state — a resume can't drive it, so
    the CLI should say WHY it dropped rather than replay an empty terminal state."""
    try:
        return RunState.load(run_dir).status == "dropped"
    except Exception:  # noqa: BLE001 — a missing/unreadable state is not a drop
        return False


def _create_run(runs_dir: Path, repo: str, sha: str | None, slug: str | None,
                *, block_copyleft: bool = True, run_config: dict | None = None,
                task_brief: str | None = None, pipeline_mode: str = "full",
                cell_model: str | None = None) -> tuple[str, str, Path, str]:
    """Resolve repo→SHA (HEAD if omitted), create the run dir, and INGEST it (synchronous). Mirrors
    POST /api/runs — including the optional per-run `run_config` (agents + bands) and `task_brief`.
    Returns (key, verdict, run_dir, reason); verdict 'exists' if the run is already present (reason
    then carries the recorded terminal drop/park reason so a resume re-surfaces WHY it parked)."""
    repo = repo.replace("https://github.com/", "").rstrip("/").removesuffix(".git")
    url = repo if repo.startswith("http") else f"https://github.com/{repo}"
    from .runkey import validate_run_key
    key = validate_run_key(slug or repo.split("/")[-1])
    run_dir = Path(runs_dir) / key
    if run_state_exists(run_dir):
        # Resuming — never touch the network just to re-derive SHA. Re-surface the last recorded
        # reason so a re-run of a DROPPED repo says WHY it dropped instead of silently replaying it.
        reason = ""
        try:
            hist = RunState.load(run_dir).history
            if hist:
                reason = hist[-1].reason or ""
        except Exception:  # noqa: BLE001 — a missing/old state must not block the resume
            pass
        return key, "exists", run_dir, reason
    sha = sha or _git_head(url)
    identity = source_identity(repo, sha)
    manifest = Manifest(run_id=f"run-{identity}", task_identity=identity, slug=key,
                        run_config=run_config, task_brief=(task_brief or None),
                        pipeline_mode=pipeline_mode, cell_model=cell_model)
    state = RunState.start(f"run-{identity}", identity, slug=key)
    res = ingest(manifest, repo, sha, work_dir=run_dir, repo_url=url, block_copyleft=block_copyleft)
    # Pass the gate's SPECIFIC reason as the FSM detail so a drop records "repo is in official
    # ProgramBench (guard)" / "license X is copyleft" — not the generic lumped fallback.
    state.advance(res.verdict, detail=res.reason)
    manifest.save(run_dir)
    state.save(run_dir)
    return key, res.verdict, run_dir, res.reason


def _cmd_fleet(args: argparse.Namespace) -> int:
    from .ui.store import RunStore
    store = RunStore(_runs_dir(args))
    sums = store.list_summaries()
    if args.json:
        print(json.dumps({"runs": [s.model_dump() for s in sums],
                          "counters": store.fleet_counters()}, indent=2))
        return 0
    if not sums:
        print(f"(no runs in {store.runs_dir})")
        return 0
    rows = []
    for s in sums:
        flag = ("paused" if s.paused else "human" if s.awaiting_human else "waiting" if s.waiting
                else "blocked" if s.blocked else (s.active_job or "")).strip()
        note = (s.active_job or s.halt_reason or "")[:54]
        rows.append((s.key, s.stage, s.status, f"{round(s.progress * 100)}%",
                     s.difficulty_pass_at_1 or "—", flag, note))
    _print_table(["RUN", "STAGE", "STATUS", "PROG", "PASS@1", "FLAG", "NOTE"], rows)
    c = store.fleet_counters()
    print(f"\n{c['total']} runs · {c['in_progress']} in-progress · {c['accepted']} accepted · "
          f"{c['blocked']} blocked · {c['dropped']} dropped · {c['paused']} paused")
    return 0


def _api_base(args: argparse.Namespace) -> str | None:
    """The server to target for run creation: `--api` > PROGRAMSMITH_API env > config.api_url. None → drive the
    local runs-dir in-process (the OSS/local default). A base makes the CLI a thin client of the same
    HTTP API the web 'New run' button uses, so the run shows up on that deployed fleet (e.g. prod)."""
    from .config import LhConfig
    explicit = getattr(args, "api", None)
    return (explicit or os.getenv("PROGRAMSMITH_API") or LhConfig.load().api_url or "").rstrip("/") or None


def _parse_agent_spec(spec: str, *, n_trials: int = 3):
    """Parse a `--smoke`/`--frontier` value: `provider/model` or `harness:provider/model`.
    The harness defaults to the credential-aware pick (`runconfig.default_local_harness`). An
    unknown harness or an un-routable model prefix fails FAST with the valid options named —
    never launches a sweep that can only error."""
    from .runconfig import HARNESSES, AgentSpec, default_local_harness, model_provider
    harness, sep, model = spec.partition(":")
    if not sep:
        harness, model = "", spec
    harness = harness or default_local_harness()
    if harness not in HARNESSES:
        raise SystemExit(
            f"unknown harness {harness!r} — choose one of: {', '.join(HARNESSES)} "
            f"(spec format: [harness:]provider/model, e.g. mini-swe:anthropic/claude-haiku-4-5)")
    if model_provider(model) is None:
        raise SystemExit(
            f"model {model!r} has no recognized provider prefix — use a litellm-style id "
            f"(anthropic/…, openai/…, gemini/…, zai/…)")
    return AgentSpec(harness=harness, model=model, n_trials=n_trials)


def _resolve_cli_run_config(args: argparse.Namespace) -> dict | None:
    """Build the per-run RunConfig from `--config <file.json>` (wins) or `--preset <name>` (a saved
    New-Run config), validated against the catalog so a bad config fails fast — parity with the UI.
    `--smoke` / `--frontier` (`[harness:]provider/model`) then override that stage's agent (over
    the file/preset, or over the built-in defaults when given alone)."""
    from .runconfig import RunConfig
    raw = getattr(args, "config", None)
    preset = getattr(args, "preset", None)
    smoke = getattr(args, "smoke", None)
    frontier = getattr(args, "frontier", None)
    if raw:
        data = json.loads(Path(raw).read_text())
    elif preset:
        from .config import LhConfig
        presets = LhConfig.load().presets or {}
        if preset not in presets:
            raise SystemExit(f"no preset {preset!r} (have: {', '.join(presets) or 'none'})")
        data = presets[preset]
    elif smoke or frontier:
        from .runconfig import default_run_config
        data = default_run_config().model_dump()
    else:
        return None
    rc = RunConfig.model_validate(data)                  # validate + normalize (raises on bad)
    for stage, spec in ((rc.difficulty, smoke), (rc.full, frontier)):
        if spec:
            n = stage.agents[0].n_trials if stage.agents else 3
            stage.agents = [_parse_agent_spec(spec, n_trials=n)]
    return rc.model_dump()


def _preflight_run_config(cfg_dict: dict | None) -> None:
    """Creation-time credential gate + the active-credential line (the SWE-gen 🔐 pattern): a run
    configured for a provider with no usable key is rejected NOW with the remediation named
    (never mid-sweep), and the operator sees exactly which harness/credential each stage will
    bill before anything runs."""
    from .preflight import credential_for, missing_provider_creds
    from .runconfig import RunConfig, default_run_config, model_provider
    missing = missing_provider_creds(cfg_dict)
    if missing:
        raise SystemExit("missing provider credentials:\n  " + "\n  ".join(missing))
    try:
        rc = RunConfig.model_validate(cfg_dict) if cfg_dict else default_run_config()
    except Exception:  # noqa: BLE001 — an invalid config is the schema validator's problem
        return
    parts = []
    for name, stage in (("smoke", rc.difficulty), ("frontier", rc.full)):
        for a in (stage.agents or []):
            _ok, detail = credential_for(a.harness, model_provider(a.model))
            parts.append(f"{name}: {a.harness} @ {a.model} ({detail})")
    if parts:
        print("🔐 " + " · ".join(parts))


def _api_post(base: str, path: str, body: dict, *, method: str = "POST") -> dict:
    """Minimal stdlib JSON client for the pipeline's own API (no new dependency)."""
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"API {method} {path} → {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"API {method} {path} unreachable at {base}: {e.reason}")


def _confirm_remote_creation(args: argparse.Namespace, *, n_runs: int = 1) -> bool:
    """Require action-time consent before handing a run to a server we do not control.

    A remote server may autodrive task-generation agents immediately, and may also have evaluation
    sweep spend enabled. The client cannot safely infer either policy before creating the run, so it
    warns conservatively. ``--yes`` remains the explicit non-interactive authorization.
    """
    from . import ux
    ux.console.print(
        "[yellow]⚠ the target server may start paid task-generation agents immediately and may "
        "also have evaluation sweeps enabled[/yellow]")
    return ux.confirm_or_abort(yes=getattr(args, "yes", False),
                               what=f"{n_runs} remote run(s)")


def _cmd_new(args: argparse.Namespace) -> int:
    cfg = _resolve_cli_run_config(args)
    base = _api_base(args)
    if base:                                          # thin client → shows up on the deployed fleet
        if not _confirm_remote_creation(args):
            return 130
        body = {"repo": args.repo, "sha": args.sha, "slug": args.slug,
                "config": cfg, "brief": args.brief,
                "mode": "draft" if args.draft else "full", "cell_model": args.cell_model}
        d = _api_post(base, "/api/runs", {k: v for k, v in body.items() if v is not None})
        print(f"[NEW] {d.get('key')}: {d.get('status', 'created')} @ {base}")
        return 0
    runs_dir = _remember_runs_dir(args)
    if not args.draft:
        _preflight_run_config(cfg)                    # solver credentials only matter for full mode
    else:
        from .preflight import credential_for
        ok, detail = credential_for("claude-code", "anthropic")
        if not ok:
            raise SystemExit(detail)
    key, verdict, run_dir, reason = _create_run(runs_dir, args.repo, args.sha, args.slug,
                                                block_copyleft=not args.allow_copyleft,
                                                run_config=cfg, task_brief=args.brief,
                                                pipeline_mode="draft" if args.draft else "full",
                                                cell_model=args.cell_model)
    note = f" — {reason}" if verdict != "pass" and reason else ""
    print(f"[NEW] {key}: ingest {verdict}{note} → {run_dir}")
    return 0 if verdict in ("pass", "exists") else 1


def _drive_ctx(args: argparse.Namespace) -> dict:
    """The foreground drive context — the SAME shape `programsmith serve`'s auto-driver builds, so a
    hero run and a served run behave identically: spend authorized (the cost preview + confirm IS
    the authorization), agentic cells as background jobs, and the config's tuning knobs. `--review`
    turns both human gates back on (ADR-0039 default is AUTO)."""
    from .config import LhConfig
    cfg = LhConfig.load()
    ctx: dict = {"sweep_live": True, "agentic": True, "agentic_background": True,
                 "difficulty_trials": cfg.difficulty_trials, "full_trials": cfg.full_trials,
                 "harden_drop_after": cfg.harden_drop_after,
                 "harden_min_improvement": cfg.harden_min_improvement}
    if cfg.ci_repo_root:
        ctx["ci_repo_root"] = cfg.ci_repo_root
    if getattr(args, "review", False):
        ctx["task_matrix_mode"] = "human"
        ctx["qa_gate_mode"] = "human"
    if getattr(args, "cell_model", None):
        ctx["model"] = args.cell_model
    if getattr(args, "draft", False):
        ctx["sweep_live"] = False
    return ctx


def _effective_rc(cfg_dict: dict | None):
    from .runconfig import RunConfig, default_run_config
    try:
        return RunConfig.model_validate(cfg_dict) if cfg_dict else default_run_config()
    except Exception:  # noqa: BLE001 — schema errors surface in the API/create validators
        return default_run_config()


def _cmd_create_hero(args: argparse.Namespace) -> int:
    """The hero one-shot: create + INGEST the run, then drive it in the FOREGROUND through every
    stage — real gates, real sweeps on your keys — narrating progress, until it parks at
    done/easy/human-gate/blocked. Resume is the default: re-running the same command picks the run
    up exactly where it parked."""
    from . import ux
    from .config import LhConfig
    cfg = _resolve_cli_run_config(args)
    base = _api_base(args)
    if base:   # server-side create: the served fleet's auto-driver takes it from here
        return _cmd_new(args)
    ux.console.print()
    if not args.draft:
        _preflight_run_config(cfg)                    # fail fast on solver creds for full mode
        ux.cost_preview(_effective_rc(cfg), trial_cost_limit=LhConfig.load().trial_cost_limit)
    else:
        from .preflight import credential_for
        ok, detail = credential_for("claude-code", "anthropic")
        if not ok:
            raise SystemExit(detail)
        draft_model = args.cell_model or LhConfig.load().default_cell_model
        ux.console.print(f"[cyan]Draft mode:[/cyan] generation with {draft_model}; stops and "
                         "exports after Static CI (0 sweeps, 0 calibration trials).")
    if not ux.confirm_or_abort(yes=args.yes, what="this run"):
        return 130
    runs_dir = _remember_runs_dir(args)
    dashboard_url: str | None = None
    if getattr(args, "dashboard", False):
        with ux.console.status("[bold]Starting dashboard…[/bold]"):
            dashboard_url = _ensure_dashboard(runs_dir, args.dashboard_host, args.dashboard_port)
        if dashboard_url:
            ux.dashboard_panel(dashboard_url, runs_dir)
    ux.console.print()
    try:
        with ux.console.status("[bold]Ingest & lock[/bold]  [dim]working…[/dim]"):
            key, verdict, run_dir, reason = _create_run(
                runs_dir, args.repo, args.sha, args.slug,
                block_copyleft=not args.allow_copyleft,
                run_config=cfg, task_brief=args.brief,
                pipeline_mode="draft" if args.draft else "full",
                cell_model=args.cell_model)
    except ValueError as e:
        ux.console.print(f"✗ {args.repo}: {e}")
        return 1
    except RuntimeError as e:   # e.g. unresolvable repo/HEAD — a typed message, never a traceback
        ux.console.print(f"✗ {args.repo}: {e}")
        ux.console.print("   check the owner/name spelling (private repos need a reachable URL); "
                         "pin a commit with --sha to skip HEAD resolution")
        return 1
    dashboard_run_url = f"{dashboard_url}/run/{key}" if dashboard_url else None
    if dashboard_run_url and getattr(args, "open_dashboard", True):
        _open_dashboard(dashboard_run_url)
    if verdict == "exists":
        ux.console.print(f"[dim]▶ {key}: run exists — resuming from where it parked[/dim]")
        # A resumed run that already DROPPED can't be driven — re-surface WHY (the recorded gate
        # reason) and how to proceed, instead of silently replaying an empty terminal state.
        if _is_dropped(run_dir):
            ux.console.print(f"[yellow]⏭ {key}: already dropped — {reason or 'see run history'}[/yellow]")
            ux.console.print("   this repo won't produce a task as-is; try a different repo, or "
                             "`programsmith dev purge-synthetic`/delete the run dir to re-ingest.")
            return 1
    elif verdict != "pass":
        out = ux.classify_halt(key, halted="terminal", stage="DROPPED", status="dropped",
                               reason=reason or f"ingest {verdict}")
        ux.console.print(f"{out.icon} {out.headline}")
        if out.advice:
            ux.console.print(f"   {out.advice}")
        return 1
    outcome = ux.drive_run_foreground(run_dir, ctx=_drive_ctx(args), interval=args.interval,
                                      notes_path=runs_dir.parent / "WORKFLOW_NOTES.md")
    ux.summary_panel({key: outcome})
    ux.next_steps_panel({key: outcome}, runs_dir=runs_dir, dashboard_url=dashboard_run_url)
    return 0 if outcome.kind in ("done", "draft", "easy", "needs-review", "paused") else 1


def _cmd_farm(args: argparse.Namespace) -> int:
    """Start MANY runs at once — and, by default, DRIVE them all to completion in the foreground
    (the 20-task sweep is one command). Each spec is `owner/name[@sha] [slug]`; read from
    positionals and/or a file (one per line, `#` comments allowed). Continues past failures with a
    typed per-run outcome; resume is the default (existing runs are picked up, not errors);
    `--no-drive` restores create+ingest-only."""
    def _read_specs_file(path: str) -> list[str]:
        out = []
        for line in Path(path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
        return out

    specs: list[str] = []
    if args.repos_file:
        specs += _read_specs_file(args.repos_file)
    for item in (args.repos or []):
        # `programsmith farm repos.txt` just works: a positional that names an existing file IS a
        # specs file (a repo spec like owner/name can never collide with an on-disk filename).
        if "/" not in item.split("@")[0] and Path(item).is_file():
            specs += _read_specs_file(item)
        else:
            specs.append(item)
    if not specs:
        print("supply repos (positional) or --repos-file <f>")
        return 2
    cfg = _resolve_cli_run_config(args)          # one shared config/preset applied to every run
    base = _api_base(args)
    drive_after = getattr(args, "drive", True) and not base
    if not base:
        if args.draft:
            from .preflight import credential_for
            ok, detail = credential_for("claude-code", "anthropic")
            if not ok:
                raise SystemExit(detail)
        else:
            _preflight_run_config(cfg)           # once for the whole farm (shared config)
    elif not _confirm_remote_creation(args, n_runs=len(specs)):
        return 130
    if drive_after:
        from . import ux
        from .config import LhConfig
        if args.draft:
            draft_model = args.cell_model or LhConfig.load().default_cell_model
            ux.console.print(f"[cyan]Draft farm:[/cyan] {len(specs)} generation run(s) with "
                             f"{draft_model}; each stops after Static CI (0 sweeps/calibration).")
        else:
            ux.cost_preview(_effective_rc(cfg), n_runs=len(specs),
                            trial_cost_limit=LhConfig.load().trial_cost_limit)
        if not ux.confirm_or_abort(yes=getattr(args, "yes", False), what=f"{len(specs)} run(s)"):
            return 130
    runs_dir = _remember_runs_dir(args) if not base else _runs_dir(args)
    rc = 0
    dest = base or runs_dir
    print(f"[FARM] {len(specs)} run(s) → {dest}")
    dashboard_url: str | None = None
    if not base and getattr(args, "dashboard", False):
        from . import ux
        with ux.console.status("[bold]Starting dashboard…[/bold]"):
            dashboard_url = _ensure_dashboard(runs_dir, args.dashboard_host, args.dashboard_port)
        if dashboard_url:
            ux.dashboard_panel(dashboard_url, runs_dir)
    keys: list[str] = []
    for spec in specs:
        parts = spec.split()
        ref, slug = parts[0], (parts[1] if len(parts) > 1 else None)
        repo, _, sha = ref.partition("@")
        try:
            if base:
                body = {"repo": repo, "sha": sha or None, "slug": slug,
                        "config": cfg, "brief": args.brief,
                        "mode": "draft" if args.draft else "full", "cell_model": args.cell_model}
                d = _api_post(base, "/api/runs", {k: v for k, v in body.items() if v is not None})
                print(f"  ✓ {d.get('key')}: {d.get('status', 'created')}")
            else:
                key, verdict, run_dir, reason = _create_run(
                    runs_dir=runs_dir, repo=repo, sha=sha or None,
                    slug=slug, block_copyleft=not args.allow_copyleft,
                    run_config=cfg, task_brief=args.brief,
                    pipeline_mode="draft" if args.draft else "full",
                    cell_model=args.cell_model)
                resumed = " (exists — resuming)" if verdict == "exists" else ""
                note = f" — {reason}" if verdict != "pass" and reason else ""
                print(f"  {'✓' if verdict in ('pass', 'exists') else '✗'} {key}: {verdict}{resumed}{note}")
                # a fresh drop, OR a resume of an already-dropped run, can't be driven — don't queue it
                if verdict == "pass" or (verdict == "exists" and not _is_dropped(run_dir)):
                    keys.append(key)
                else:
                    rc = rc or 1
        except SystemExit as e:  # API error for one repo must not abort the farm
            print(f"  ✗ {spec}: {e}")
            rc = 1
        except Exception as e:  # noqa: BLE001 — one bad repo must not abort the farm
            print(f"  ✗ {spec}: {e}")
            rc = 1
    if not drive_after or not keys:
        return rc
    from . import ux
    outcomes = ux.drive_fleet_foreground(
        runs_dir, keys, ctx=_drive_ctx(args), interval=args.interval,
        prune=getattr(args, "prune", True),
        notes_path=runs_dir.parent / "WORKFLOW_NOTES.md")
    ux.summary_panel(outcomes)
    ux.next_steps_panel(outcomes, runs_dir=runs_dir, dashboard_url=dashboard_url)
    bad = sum(1 for o in outcomes.values() if o.kind in ("error", "blocked"))
    return rc or (1 if bad else 0)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Preflight as a first-class command (the SWE-gen 'doctor' pattern): Docker, the required
    Anthropic credential, disk headroom, and the per-provider table of optional solver families."""
    from . import ux
    from .preflight import check_preflight
    return ux.doctor_report(check_preflight())


def _cmd_retry(args: argparse.Namespace) -> int:
    """Clear errored/orphaned background job(s) AND errored sweep records for a run so the next
    drive pass relaunches them fresh — same semantics as POST /runs/{key}/retry."""
    from .jobs import clear_errored_jobs
    from .orchestrator import clear_errored_sweeps
    run_dir = _run_dir_for(args)
    if not run_state_exists(run_dir):
        print(f"no run {args.key!r}")
        return 2
    cleared = clear_errored_jobs(run_dir)
    try:
        cleared += [f"sweep[{k}]" for k in clear_errored_sweeps(Manifest.load(run_dir), run_dir)]
    except FileNotFoundError:   # pre-INGEST run: no manifest yet, so no sweeps to clear
        pass
    print(f"[RETRY] {args.key}: cleared {len(cleared)} errored job(s)/sweep(s)" +
          (f" ({', '.join(cleared)})" if cleared else " — nothing was errored") +
          "; the next pass relaunches fresh (`programsmith create`/`serve` drives it)")
    return 0


def _cmd_reopen(args: argparse.Namespace) -> int:
    """Re-open a terminal (dropped/blocked/easy) run for another harden attempt with a fresh
    budget — same semantics as POST /runs/{key}/reopen."""
    run_dir = _run_dir_for(args)
    try:
        st = RunState.load(run_dir)
    except FileNotFoundError:
        print(f"no run {args.key!r}")
        return 2
    try:
        st.reopen_for_harden()
    except ValueError as e:
        print(f"[REOPEN] {args.key}: {e}")
        return 1
    st.save(run_dir)
    print(f"[REOPEN] {args.key}: re-entered at {st.current_stage.value} (harden={st.harden}) — "
          "re-run `programsmith create`/`serve` to drive it")
    return 0


def _cmd_reaudit_sources(args: argparse.Namespace) -> int:
    """Dry-run or reversibly requeue high-confidence source-screen false negatives."""
    from .source_reaudit import apply_reaudit, build_reaudit_plan

    runs_dir = _remember_runs_dir(args)
    plan = build_reaudit_plan(runs_dir)
    include = set(args.include or [])
    retry = [item for item in plan if item["decision"] == "retry"]
    if include:
        retry = [item for item in retry if item["key"] in include]
    if args.limit is not None:
        retry = retry[: args.limit]
    if args.apply:
        for item in retry:
            apply_reaudit(runs_dir / item["key"], item)
    payload = {
        "mode": "apply" if args.apply else "dry-run",
        "source_rejections": len(plan),
        "high_confidence_retries": len(retry),
        "kept_rejected": len(plan) - sum(item["decision"] == "retry" for item in plan),
        "items": retry,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        action = "Requeued" if args.apply else "Would requeue"
        print(f"{action} {len(retry)} high-confidence source(s); "
              f"{payload['source_rejections']} source rejection(s) audited.")
        for item in retry:
            print(f"  {item['key']}  score={item['review_score']}  "
                  f"{', '.join(item['positive_signals'][:6])}")
        if not args.apply and retry:
            print("\nApply with: programsmith reaudit-sources --apply")
    return 0


def _cmd_presets(args: argparse.Namespace) -> int:
    """List / save / delete saved New-Run configs (parity with the New Run dialog's preset picker). Operates on
    the deployed server (`--api`) or the local config."""
    from .config import LhConfig
    from .runconfig import RunConfig
    base = _api_base(args)
    if args.save:
        if not args.config:
            print("--save needs --config <file.json>")
            return 2
        norm = RunConfig.model_validate(json.loads(Path(args.config).read_text())).model_dump()
        if base:
            _api_post(base, "/api/presets", {"name": args.save, "config": norm})
        else:
            c = LhConfig.load(); c.presets = {**(c.presets or {}), args.save: norm}; c.save()
        print(f"[PRESET] saved {args.save!r}")
        return 0
    if args.delete:
        if base:
            _api_post(base, f"/api/presets/{args.delete}", None, method="DELETE")
        else:
            c = LhConfig.load(); (c.presets or {}).pop(args.delete, None); c.save()
        print(f"[PRESET] deleted {args.delete!r}")
        return 0
    presets = (_api_post(base, "/api/presets", None, method="GET").get("presets")
               if base else LhConfig.load().presets) or {}
    if not presets:
        print("(no presets)")
        return 0
    for name in presets:
        print(f"  {name}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from .orchestrator import peek
    from .ui.store import RunStore
    store = RunStore(_runs_dir(args))
    run_dir = store.runs_dir / args.key
    if not run_state_exists(run_dir):
        print(f"no run {args.key!r} in {store.runs_dir}")
        return 2
    summ = store.summary(args.key)
    st, man = store.get_state(args.key), store.get_manifest(args.key)
    try:
        waiting = peek(run_dir)
    except FileNotFoundError:
        waiting = {"kind": "?", "reason": ""}
    if summ.active_job:
        # `peek` is deliberately side-effect free and has no execution context, so an agentic stage
        # can otherwise say "needs an execution env" even while another foreground CLI/server
        # process is actively running its job. The persisted live job is the authoritative status.
        waiting = {"stage": summ.stage, "kind": "waiting",
                   "reason": f"{summ.active_job}: background job running"}
    if args.json:
        print(json.dumps({"summary": summ.model_dump(),
                          "history": [e.model_dump() for e in st.history],
                          "sweeps": (man.sweeps if man else {}), "waiting": waiting},
                         indent=2, default=str))
        return 0
    print(f"{args.key} — {summ.stage} ({summ.status}) · {round(summ.progress * 100)}% · "
          f"harden {summ.harden} revise {summ.revise}")
    if man and man.source:
        print(f"  source     : {man.source.repo}@{man.source.pinned_sha[:12]}")
    if summ.difficulty_pass_at_1:
        print(f"  difficulty : pass@1 {summ.difficulty_pass_at_1}")
    if summ.full_sweep_band:
        print(f"  full sweep : {summ.full_sweep_band}")
    print(f"  waiting    : [{waiting['kind']}] {waiting['reason']}")
    for stage, entry in ((man.sweeps if man else {}) or {}).items():
        exp = entry.get("experiment") if isinstance(entry, dict) else None
        if exp:
            print(f"  sweep[{stage}]: {exp}")
    print("  history:")
    for ev in st.history:
        print(f"    {ev.stage.value} --{ev.verdict}--> {ev.next.value}  ({ev.reason})")
    return 0


def _cmd_advance(args: argparse.Namespace) -> int:
    """Drive a run (or the whole fleet with --all) one pass, on REAL results. --spend authorizes
    billable sweeps to launch; --ci-repo-root overrides the STATIC CI check suite. Mirrors the
    served auto-driver."""
    from .daemon import autodrive_once
    ctx: dict = {}
    if args.spend:
        ctx["sweep_live"] = True
    if args.ci_repo_root:
        ctx["ci_repo_root"] = args.ci_repo_root
    runs_dir = _runs_dir(args)
    notes = Path(runs_dir).parent / "WORKFLOW_NOTES.md"
    if args.all:
        recs = autodrive_once(runs_dir, ctx=ctx, notes_path=notes)
        for r in recs:
            print(f"  {r['key']:16} +{r['advanced']} → {r['final_stage']} "
                  f"[{r['halted']}] {r['halt_reason'][:70]}")
        if not recs:
            print("(no eligible runs)")
        return 0
    if not args.key:
        print("supply a run KEY or --all")
        return 2
    res = drive(runs_dir / args.key, ctx=ctx, notes_path=notes)
    for s in res.steps:
        print(f"  ✓ {s['stage']} --{s['verdict']}--> {s['next']}  ({s['reason']})")
    if not res.steps:
        print("  (no stages advanced)")
    print(f"halted [{res.halted}] at {res.final_stage}: {res.halt_reason}")
    return 0


def _cmd_matrix(args: argparse.Namespace) -> int:
    """HUMAN REVIEW #1 helper — run the TASK MATRIX cell for a run KEY and print candidates."""
    from .config import LhConfig
    run_dir = _run_dir_for(args)
    manifest = Manifest.load(run_dir)
    # Light one-shot annotation → cell_model_light (ADR-0042), never the heavy default.
    out: TaskMatrixOutput = propose(manifest, model=args.model or LhConfig.load().cell_model_light)
    (run_dir / TASK_MATRIX_FILE).write_text(out.model_dump_json(indent=2) + "\n")
    print(f"[MATRIX] {len(out.candidates)} candidate(s) for {out.source_ref}:")
    for i, c in enumerate(out.candidates):
        print(f"  [{i}] {c.tool_name} · {c.upstream_language} · ~{c.est_kloc} kLOC · "
              f"diff={c.expected_difficulty} · {c.recommendation}")
    print(f"\nSelect with:  programsmith pick {args.key} --index <i>   (or --none)")
    return 0


def _cmd_pick(args: argparse.Namespace) -> int:
    """HUMAN REVIEW #1 selection by run KEY (records the chosen candidate, or --none to drop).
    A manual override: with task_matrix_mode=auto (ADR-0039) the driver picks for itself, but a run
    parked at TASK_MATRIX (human mode, or auto before the driver's pass) still accepts this."""
    run_dir = _run_dir_for(args)
    manifest = Manifest.load(run_dir)
    state = RunState.load(run_dir)
    if args.none:
        state.advance("none_selected")
        state.save(run_dir)
        print(f"[PICK] none → {state.current_stage.value} ({state.status})")
        return 0
    if args.index is None:
        print("supply --index <i> or --none")
        return 2
    out = TaskMatrixOutput.model_validate_json((run_dir / TASK_MATRIX_FILE).read_text())
    c = out.candidates[args.index]
    apply_selection(manifest, c)   # ONE shared code path with the UI pick + the auto-pick handler
    state.task_identity = manifest.task_identity
    state.advance("selected")
    manifest.save(run_dir)
    state.save(run_dir)
    print(f"[PICK] [{args.index}] {c.tool_name} ({c.flag_surface}) → {state.current_stage.value}")
    return 0


def _cmd_qa_gate(args: argparse.Namespace) -> int:
    """HUMAN REVIEW #2 by run KEY — accept | revise | reject (authoritative). Only meaningful in
    human mode: with qa_gate_mode=auto (the ADR-0039 default) the gate decides itself from the
    recorded sweeps, so a manual verdict here would race/override the auto decision."""
    from .config import LhConfig
    from .workflow_notes import record_backward_move
    if LhConfig.load().qa_gate_mode != "human":
        print("qa_gate_mode=auto — the gate decides itself; set qa_gate_mode=human to review manually")
        return 2
    run_dir = _run_dir_for(args)
    state = RunState.load(run_dir)
    if state.current_stage.value != "QA_GATE":
        print(f"run is at {state.current_stage.value}, not QA_GATE")
        return 2
    if args.decision not in ("accept", "revise", "reject"):
        print("decision must be accept | revise | reject")
        return 2
    dec = state.advance(args.decision)
    record_backward_move(state, dec, trigger=f"qa_gate: human {args.decision}",
                         notes_path=_runs_dir(args).parent / "WORKFLOW_NOTES.md",
                         what_failed="human review #2")
    state.save(run_dir)
    print(f"[QA GATE] {args.decision} → {state.current_stage.value} ({state.status})")
    if args.decision == "accept":
        # accept → DONE is a pure FSM transition; export the task to the outbox (the pipeline's
        # only output) the same way the auto handler does inline — else a human-accepted task
        # reports DONE while producing nothing.
        from .orchestrator import export_on_human_accept
        _dest, note = export_on_human_accept(run_dir)
        print(f"[QA GATE] {note}")
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    from .ui.store import RunStore
    st = RunStore(_runs_dir(args)).set_paused(args.key, not args.resume)
    print(f"[{'RESUME' if args.resume else 'PAUSE'}] {args.key}: paused={st.paused}")
    return 0


def _print_tree(node: dict, prefix: str = "") -> None:
    kids = node.get("children", [])
    for i, c in enumerate(kids):
        last = i == len(kids) - 1
        branch = "└── " if last else "├── "
        size = "" if c["type"] == "dir" else f"  ({c.get('size', 0)}b)"
        print(f"{prefix}{branch}{c['name']}{'/' if c['type'] == 'dir' else ''}{size}")
        if c["type"] == "dir":
            _print_tree(c, prefix + ("    " if last else "│   "))
    if node.get("truncated"):
        print(f"{prefix}… (truncated)")


def _cmd_files(args: argparse.Namespace) -> int:
    from .ui import files as fb
    t = fb.tree(_run_dir_for(args), args.path or "")
    print(f"{t['name']}/")
    _print_tree(t)
    return 0


def _cmd_cat(args: argparse.Namespace) -> int:
    from .ui import files as fb
    try:
        out = fb.read(_run_dir_for(args), args.path)
    except FileNotFoundError:
        print(f"no file {args.path!r}")
        return 2
    except ValueError:
        print("invalid path")
        return 2
    if out["kind"] == "text":
        print(out["content"])
    else:
        print(f"[{out['kind']} · {out.get('size', 0)} bytes] {out['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="programsmith",
        description="ProgramSmith — turn CLI-tool repos into difficulty-calibrated ProgramBench tasks")
    from . import __version__
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_runs_dir(sp):  # shared --runs-dir option (defaults to config.runs_dir)
        sp.add_argument("--runs-dir", default=None, help="fleet directory (default: config.runs_dir)")

    def _add_new_run_flags(np):
        # The full set the web "New run" button supports — so the CLI has parity.
        np.add_argument("--brief", default=None, help="task brief to steer the TASK MATRIX agent")
        np.add_argument("--draft", action="store_true",
                        help="export after Static CI; launch no sweeps or calibration")
        np.add_argument("--cell-model", default=None,
                        help="model for all task-generation cells (e.g. claude-opus-4-8)")
        np.add_argument("--config", default=None, help="RunConfig JSON file (agents + per-model bands)")
        np.add_argument("--preset", default=None, help="a saved preset name (see `programsmith presets`)")
        np.add_argument("--smoke", default=None, metavar="[HARNESS:]PROVIDER/MODEL",
                        help="smoke-sweep agent override, e.g. anthropic/claude-haiku-4-5 or "
                             "mini-swe:zai/glm-5.2 (harness defaults to the credential-aware pick)")
        np.add_argument("--frontier", default=None, metavar="[HARNESS:]PROVIDER/MODEL",
                        help="frontier-sweep agent override, e.g. anthropic/claude-opus-4-8 or "
                             "gemini-cli:gemini/gemini-3.1-pro-preview")
        np.add_argument("--api", default=None, metavar="URL",
                        help="target a running server's API (e.g. the prod URL) so the run lands on that "
                             "deployed fleet; default drives the local runs-dir in-process")

    def _add_drive_flags(dp):
        # Shared by the foreground hero commands (create / farm).
        dp.add_argument("--yes", "-y", action="store_true",
                        help="skip the cost-preview confirmation (CI / scripted use)")
        dp.add_argument("--review", action="store_true",
                        help="turn the two human gates ON for this invocation (default: AUTO — "
                             "zero-touch end-to-end)")
        dp.add_argument("--interval", type=float, default=8.0,
                        help="poll interval while a sweep/agent job is in flight (seconds)")
        dp.add_argument("--dashboard", dest="dashboard", action="store_true", default=True,
                        help="auto-start the local dashboard (view-only) and print a live link "
                             "(default: on)")
        dp.add_argument("--no-dashboard", dest="dashboard", action="store_false",
                        help="do not auto-start the dashboard")
        dp.add_argument("--dashboard-port", type=int, default=8765,
                        help="port for the auto-started dashboard (default: 8765)")
        dp.add_argument("--dashboard-host", default="127.0.0.1",
                        help="host for the auto-started dashboard (default: 127.0.0.1)")
        dp.add_argument("--open-dashboard", dest="open_dashboard", action="store_true", default=True,
                        help="open the task dashboard in your browser once the run is created (default: on)")
        dp.add_argument("--no-open-dashboard", dest="open_dashboard", action="store_false",
                        help="start the dashboard without opening a browser tab")

    # ---- hero commands (the README surface) ----
    pcre = sub.add_parser(
        "create", help="one repo → one calibrated task, foreground (create + ingest + drive to done)")
    pcre.add_argument("--repo", required=True, help="owner/name or full URL")
    pcre.add_argument("--sha", default=None, help="pin a commit (default: resolve HEAD)")
    pcre.add_argument("--slug", default=None, help="run key (default: the repo name)")
    pcre.add_argument("--allow-copyleft", action="store_true")
    _add_new_run_flags(pcre)
    _add_drive_flags(pcre)
    _add_runs_dir(pcre)
    pcre.set_defaults(func=_cmd_create_hero)

    pdoc = sub.add_parser("doctor", help="preflight: Docker, credentials (per provider), disk")
    pdoc.set_defaults(func=_cmd_doctor)

    # ---- dev/plumbing commands (per-stage cells + gates; power users and tests) ----
    pdev = sub.add_parser("dev", help="low-level per-stage commands (cells, gates, sweep imports)")
    devsub = pdev.add_subparsers(dest="devcmd", required=True)

    pi = devsub.add_parser("ingest", help="INGEST + LOCK a source repo at a pinned SHA")
    pi.add_argument("--repo", required=True, help="owner/name")
    pi.add_argument("--sha", required=True)
    pi.add_argument("--url", default=None, help="clone URL (default https://github.com/{repo})")
    pi.add_argument("--local", default=None, help="use a local checkout instead of cloning")
    pi.add_argument("--run-dir", required=True)
    pi.add_argument("--run-id", default=None)
    pi.add_argument("--slug", default=None)
    pi.add_argument("--ts", default=None)
    pi.add_argument("--allow-copyleft", action="store_true", help="do not drop copyleft sources")
    pi.set_defaults(func=_cmd_ingest)

    pt = devsub.add_parser("task-matrix", help="run the TASK MATRIX cell (human review #1)")
    pt.add_argument("--run-dir", required=True)
    pt.add_argument("--model", default=None, help="override the subscription model id")
    pt.set_defaults(func=_cmd_task_matrix)

    psel = devsub.add_parser("select", help="record human review #1 selection")
    psel.add_argument("--run-dir", required=True)
    psel.add_argument("--pick", type=int, default=None, help="candidate index to run")
    psel.add_argument("--none", action="store_true", help="select no candidate (drops the run)")
    psel.add_argument("--ts", default=None)
    psel.set_defaults(func=_cmd_select)

    po = devsub.add_parser("oracle-golden", help="ORACLE+GOLDEN cell (adopt a bundle, or --generate)")
    po.add_argument("--run-dir", required=True)
    po.add_argument("--bundle", required=True, help="dir with oracle/ + goldens/{public,heldout}.json "
                    "(adopt: read; --generate: write target)")
    po.add_argument("--generate", action="store_true",
                    help="agentic clean-room generate-mode (needs an execution env)")
    po.add_argument("--max-iters", type=int, default=3)
    po.add_argument("--model", default=None, help="override the agentic-session model")
    po.add_argument("--ts", default=None)
    po.set_defaults(func=_cmd_oracle_golden)

    pcr = devsub.add_parser("create", help="CREATE cell (assemble the hybrid task skeleton; --fill)")
    pcr.add_argument("--run-dir", required=True)
    pcr.add_argument("--out", default=None, help="output task dir (default <run-dir>/task/<slug>)")
    pcr.add_argument("--fill", action="store_true",
                     help="run the agentic fill loop after assembly (needs an execution env)")
    pcr.add_argument("--max-iters", type=int, default=3)
    pcr.add_argument("--model", default=None, help="override the agentic-session model")
    pcr.add_argument("--ts", default=None)
    pcr.set_defaults(func=_cmd_create)

    psy = devsub.add_parser("synthesize", help="SYNTHESIZE/REVISE patch plan (LLM); --apply to drive it")
    psy.add_argument("--run-dir", required=True)
    psy.add_argument("--task-dir", default=None)
    psy.add_argument("--move", choices=["harden", "ease", "revise"], required=True)
    psy.add_argument("--from-stage", choices=["CALIBRATE", "QA_PROBE", "FULL_SWEEP", "QA_GATE"],
                     required=True)
    psy.add_argument("--reason", required=True, help="what failed / what to close")
    psy.add_argument("--finding", action="append", help="a finding to close (repeatable; e.g. H1)")
    psy.add_argument("--apply", action="store_true", help="drive the agentic apply loop (exec env)")
    psy.add_argument("--max-iters", type=int, default=2)
    psy.add_argument("--model", default=None, help="override the model")
    psy.set_defaults(func=_cmd_synthesize)

    psan = devsub.add_parser("sanity", help="SANITY gate (docker build + oracle=1/nop=0/priv-drop)")
    psan.add_argument("--task-dir", required=True, help="a Harbor task directory")
    psan.add_argument("--tag", default="lh-sanity:task")
    psan.add_argument("--no-build", action="store_true", help="reuse an existing image tag")
    psan.add_argument("--run-dir", default=None)
    psan.add_argument("--advance", action="store_true")
    psan.add_argument("--ts", default=None)
    psan.set_defaults(func=_cmd_sanity)

    pc = devsub.add_parser("static-ci", help="STATIC CI gate (replay the CHECK_ORDER)")
    pc.add_argument("--repo-root", required=True, help="a directory with ci_checks/ (the drive path "
                    "stages the vendored suite automatically)")
    pc.add_argument("--task", required=True, help="task dir relative to repo-root, e.g. tasks/<slug>")
    pc.add_argument("--run-dir", default=None)
    pc.add_argument("--advance", action="store_true", help="advance the FSM if at STATIC_CI")
    pc.add_argument("--ts", default=None)
    pc.set_defaults(func=_cmd_static_ci)

    psr = devsub.add_parser("sweep-read",
                         help="import trial records (artifacts dir / status JSON) → record band + verdict")
    psr.add_argument("--run-dir", required=True)
    psr.add_argument("--kind", choices=["sanity", "difficulty", "qa_probe", "full"], required=True)
    psr.add_argument("--from-pull", default=None, help="dir of trial artifacts (result.json…)")
    psr.add_argument("--status-json", default=None, help="a status-payload JSON file ({'trials': […]})")
    psr.add_argument("--experiment", default=None,
                     help="experiment handle to record alongside the trials (provenance)")
    psr.add_argument("--advance", action="store_true", help="advance the FSM if at the matching stage")
    psr.add_argument("--ts", default=None)
    psr.set_defaults(func=_cmd_sweep_read)

    prun = devsub.add_parser("run", help="drive a run through its runnable stages until it halts")
    prun.add_argument("--run-dir", required=True)
    prun.add_argument("--oracle-bundle", default=None, help="reference bundle for ORACLE+GOLDEN adopt")
    prun.add_argument("--ci-repo-root", default=None, help="STATIC CI check-suite override (default: vendored)")
    prun.add_argument("--task-path", default=None, help="complete task bundle to sweep")
    prun.add_argument("--spend", action="store_true",
                      help="authorize real billable sweeps to launch (bills your own key)")
    prun.add_argument("--max-steps", type=int, default=50)
    prun.set_defaults(func=_cmd_run)

    pad = devsub.add_parser("autodrive", help="advance the whole fleet hands-free up to each run's next gate")
    pad.add_argument("--runs-dir", default=".programsmith/runs")
    pad.add_argument("--interval", type=float, default=5.0, help="seconds between passes")
    pad.add_argument("--once", action="store_true", help="run a single pass and exit")
    pad.add_argument("--max-passes", type=int, default=None)
    pad.add_argument("--ci-repo-root", default=None, help="optional fleet-wide STATIC CI check-suite override")
    pad.add_argument("--spend", action="store_true",
                     help="authorize real billable sweeps to launch (bills your own key)")
    pad.add_argument("--verbose", action="store_true", help="report runs even when nothing advanced")
    pad.set_defaults(func=_cmd_autodrive)

    psrv = sub.add_parser("serve", help="start the local dashboard in the background")
    psrv.add_argument("--runs-dir", default=None,
                      help="fleet directory (default: the last one used by create/farm, or config)")
    psrv.add_argument("--host", default="127.0.0.1")
    psrv.add_argument("--port", type=int, default=8765)
    psrv.add_argument("--autodrive", action=argparse.BooleanOptionalAction, default=True,
                      help="run the fleet auto-driver in the background so runs flow forward on REAL "
                           "results, halting at the human gates (default: on; --no-autodrive to disable)")
    psrv.add_argument("--autodrive-interval", type=float, default=8.0)
    psrv.add_argument("--spend", action=argparse.BooleanOptionalAction, default=False,
                      help="authorize evaluation solver sweeps on your provider keys (default: off). "
                           "Task-generation cells still use the Anthropic credential when autodrive is on")
    psrv.add_argument("--ci-repo-root", default=None, help="fleet-wide STATIC CI check-suite override")
    psrv.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    psrv.set_defaults(func=_cmd_serve)

    pstop = sub.add_parser("stop", help="stop the background dashboard")
    pstop.set_defaults(func=_cmd_stop)

    ps = devsub.add_parser("status", help="show a run's FSM state + history")
    ps.add_argument("--run-dir", required=True)
    ps.set_defaults(func=_cmd_status)

    pps = devsub.add_parser("purge-synthetic",
                         help="strip synthetic data + roll a run back to its last REAL stage")
    pps.add_argument("--run-dir", required=True)
    pps.set_defaults(func=_cmd_purge_synthetic)

    # ---- fleet-level commands (key-based, resolve against --runs-dir; everything the UI does) ----
    pfl = sub.add_parser("fleet", help="list every run + fleet counters (the UI Fleet page)")
    _add_runs_dir(pfl)
    pfl.add_argument("--json", action="store_true")
    pfl.set_defaults(func=_cmd_fleet)

    pst = sub.add_parser("status", help="full run detail by KEY (stage, sweeps, history)")
    pst.add_argument("key")
    pst.add_argument("--json", action="store_true")
    _add_runs_dir(pst)
    pst.set_defaults(func=_cmd_show)

    pret = sub.add_parser("retry", help="clear errored jobs so a blocked stage relaunches fresh")
    pret.add_argument("key")
    _add_runs_dir(pret)
    pret.set_defaults(func=_cmd_retry)

    prop = sub.add_parser("reopen", help="re-open a terminal run for another harden attempt")
    prop.add_argument("key")
    _add_runs_dir(prop)
    prop.set_defaults(func=_cmd_reopen)

    prea = sub.add_parser(
        "reaudit-sources",
        help="re-evaluate high-confidence sources rejected before task generation (dry-run by default)",
    )
    _add_runs_dir(prea)
    prea.add_argument("--apply", action="store_true",
                      help="requeue selected sources at TASK_MATRIX (old decisions are backed up)")
    prea.add_argument("--include", action="append", default=None, metavar="KEY",
                      help="only requeue this run key (repeatable)")
    prea.add_argument("--limit", type=int, default=None,
                      help="maximum high-confidence sources to requeue")
    prea.add_argument("--json", action="store_true")
    prea.set_defaults(func=_cmd_reaudit_sources)

    pnew = devsub.add_parser("new", help="create + INGEST a run from owner/name (resolves HEAD if no --sha) "
                                         "without driving it — `create` is the foreground form")
    pnew.add_argument("--repo", required=True, help="owner/name or full URL")
    pnew.add_argument("--sha", default=None)
    pnew.add_argument("--slug", default=None, help="run key (default: the repo name)")
    pnew.add_argument("--allow-copyleft", action="store_true")
    _add_new_run_flags(pnew)
    pnew.add_argument("--yes", "-y", action="store_true",
                      help="authorize creating the run on a server that may start paid model work")
    _add_runs_dir(pnew)
    pnew.set_defaults(func=_cmd_new)

    pfarm = sub.add_parser(
        "farm",
        help="many repos → many calibrated tasks: create + INGEST, then drive them "
             "all to completion in the foreground (--no-drive to only enqueue)")
    pfarm.add_argument("repos", nargs="*", help="specs: owner/name[@sha] [slug]")
    pfarm.add_argument("--repos-file", default=None, help="file of specs, one per line (# comments ok)")
    pfarm.add_argument("--allow-copyleft", action="store_true")
    pfarm.add_argument("--drive", action=argparse.BooleanOptionalAction, default=True,
                     help="drive the created runs to completion in the foreground (default: on)")
    pfarm.add_argument("--prune", action=argparse.BooleanOptionalAction, default=True,
                     help="docker image prune (dangling only) between completed runs (default: on)")
    _add_new_run_flags(pfarm)
    _add_drive_flags(pfarm)
    _add_runs_dir(pfarm)
    pfarm.set_defaults(func=_cmd_farm)

    ppre = sub.add_parser("presets", help="list / save / delete saved New-Run configs")
    ppre.add_argument("--save", default=None, metavar="NAME", help="save --config as this preset name")
    ppre.add_argument("--delete", default=None, metavar="NAME", help="delete a preset")
    ppre.add_argument("--config", default=None, help="RunConfig JSON file (with --save)")
    ppre.add_argument("--api", default=None, metavar="URL", help="target a running server's API")
    ppre.set_defaults(func=_cmd_presets)

    padv = devsub.add_parser("advance", help="drive a run one pass (or --all for the whole fleet)")
    padv.add_argument("key", nargs="?", default=None)
    padv.add_argument("--all", action="store_true", help="advance every eligible run one pass")
    padv.add_argument("--spend", action="store_true", help="authorize billable sweeps to launch")
    padv.add_argument("--ci-repo-root", default=None, help="STATIC CI check-suite override (default: vendored)")
    _add_runs_dir(padv)
    padv.set_defaults(func=_cmd_advance)

    pmx = devsub.add_parser("matrix", help="run TASK MATRIX for a run KEY (human review #1)")
    pmx.add_argument("key")
    pmx.add_argument("--model", default=None)
    _add_runs_dir(pmx)
    pmx.set_defaults(func=_cmd_matrix)

    ppk = sub.add_parser("pick", help="record the TASK MATRIX selection by run KEY (review #1)")
    ppk.add_argument("key")
    ppk.add_argument("--index", type=int, default=None, help="candidate index to run")
    ppk.add_argument("--none", action="store_true", help="select nothing (drops the run)")
    _add_runs_dir(ppk)
    ppk.set_defaults(func=_cmd_pick)

    pqg = sub.add_parser("qa-gate", help="record HUMAN REVIEW #2 by run KEY (accept|revise|reject)")
    pqg.add_argument("key")
    pqg.add_argument("--decision", required=True, choices=["accept", "revise", "reject"])
    _add_runs_dir(pqg)
    pqg.set_defaults(func=_cmd_qa_gate)

    ppa = sub.add_parser("pause", help="operationally pause/resume a run by KEY")
    ppa.add_argument("key")
    ppa.add_argument("--resume", action="store_true", help="resume instead of pausing")
    _add_runs_dir(ppa)
    ppa.set_defaults(func=_cmd_pause)

    pfi = devsub.add_parser("files", help="print a run's working-dir tree (task, source, artifacts)")
    pfi.add_argument("key")
    pfi.add_argument("path", nargs="?", default="", help="subtree to scope to")
    _add_runs_dir(pfi)
    pfi.set_defaults(func=_cmd_files)

    pca = devsub.add_parser("cat", help="preview a file under a run's working dir")
    pca.add_argument("key")
    pca.add_argument("path")
    _add_runs_dir(pca)
    pca.set_defaults(func=_cmd_cat)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
