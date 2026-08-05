"""Read layer for the UI — load run state + manifest from a runs directory.

A "run" is a subdirectory containing `state.json` (+ optionally `manifest.json`). Pure reads;
the only mutation is pause/resume, which flips `STATE.paused` and re-saves. The UI is fed entirely
by these two files (the FSM tracker is the source of truth).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..fsm import Stage
from ..jobs import active_job
from ..manifest import Manifest
from ..state import RunState

# Canonical forward DAG order for the run view (SYNTHESIZE is rendered as a side/loop node).
# QA_ON_GPT/PR are legacy drains (ADR-0039) — never rendered as forward nodes anymore.
FORWARD: list[Stage] = [
    Stage.INGEST_LOCK, Stage.TASK_MATRIX, Stage.ORACLE_GOLDEN, Stage.CREATE, Stage.SANITY,
    Stage.STATIC_CI, Stage.DIFFICULTY_SWEEP, Stage.CALIBRATE, Stage.QA_PROBE, Stage.FULL_SWEEP,
    Stage.QA_GATE,
]
HUMAN_GATE_STAGES = {Stage.TASK_MATRIX, Stage.QA_GATE}


def _active_human_stages() -> frozenset[Stage]:
    """The gates that ACTUALLY block for a human, given the current task_matrix_mode/qa_gate_mode
    (ADR-0039: both default auto → empty set). Delegates to the orchestrator's canonical helper so
    the fleet view agrees with what the driver actually does; falls back to the static gate set if
    the orchestrator import isn't available (keeps the read path robust)."""
    try:
        from ..orchestrator import active_human_stages
        return active_human_stages()
    except Exception:  # noqa: BLE001 — display helper must never break the fleet read
        return frozenset(HUMAN_GATE_STAGES)


def _fmt_pa(v: object) -> str:
    """Compact pass@1 for the fleet band string: a percentage when numeric, '—' when unmeasured."""
    if isinstance(v, (int, float)):
        return f"{round(v * 100)}%"
    return "—"


_FAMILY_ABBREV = {"claude-code": "cc", "codex": "cx", "gemini-cli": "gem", "mini-swe": "mini",
                  "mini-swe-agent": "mini"}


def _fam_abbrev(family: str) -> str:
    """Short family label for the run-card band chip (keeps an N-family string scannable)."""
    return _FAMILY_ABBREV.get(family, family)


class RunSummary(BaseModel):
    key: str           # the run directory name (URL handle)
    run_id: str
    slug: str | None
    stage: str
    status: str       # FSM status plus the read-layer "draft" completion state
    paused: bool
    harden: int
    revise: int
    difficulty_pass_at_1: str | None = None
    full_sweep_band: str | None = None   # "cc=… cx=…" full-sweep band, when measured
    progress: float = 0.0  # 0..1 fraction of the forward DAG reached (furthest stage; not stage idx
    # alone, so a run looping at SYNTHESIZE still shows real progress instead of 0%)
    awaiting_human: bool = False
    active_job: str | None = None   # any running bg job (ingest | task_matrix | oracle-generate | …)
    # Operational halt: a handler returned blocked=True (Docker/spend/missing-input/agent-error). The
    # FSM status stays "in_progress" (no terminal transition), so the fleet needs this to SHOW a run
    # is stuck and WHY — otherwise a blocked run looks like it's progressing.
    blocked: bool = False
    # A BENIGN in-flight wait: the run is only halted because it's polling a launched sweep
    # (or within its bounded auto-retry). It advances itself when the sweep lands — so the UI shows
    # "waiting", not a red "blocked" badge. Mutually exclusive with `blocked`.
    waiting: bool = False
    halt_reason: str | None = None
    # A finished, ACCEPTED task (DONE): QA_GATE accepted it and the driver exported it to the outbox
    # (ADR-0039 — no PR is opened). Rendered as the green "Exported" state.
    exported: bool = False
    # DEPRECATED alias of `exported` — kept emitting so an un-migrated frontend build keeps rendering
    # finished runs as success instead of regressing to a blank state. Remove once the SPA reads
    # `exported`.
    ready_for_pr: bool = False
    created_by: str | None = None   # the operator who kicked off the run (WS5), when the fleet is authed
    # True when TASK_MATRIX rejected the repository before ORACLE/CREATE. This is source curation,
    # not a task-pipeline failure, and is presented separately from downstream DROPPED outcomes.
    screened_out: bool = False
    # True once TASK_MATRIX selected a candidate. The default fleet view shows admitted tasks and
    # keeps source discovery/screening in its own filters and funnel.
    source_admitted: bool = False


class RunStore:
    def __init__(self, runs_dir: str | Path):
        self.runs_dir = Path(runs_dir)

    def _run_dirs(self) -> list[Path]:
        # Run keys come from the StateStore (the runs-root listing); the returned run_dir Paths
        # feed the wired RunState/Manifest loads.
        from ..statestore import get_store
        store = get_store(self.runs_dir)
        keys = [k for k in store.list_dir("") if store.exists(f"{k}/state.json")]
        return [self.runs_dir / k for k in sorted(keys)]

    def get_state(self, key: str) -> RunState:
        return RunState.load(self.runs_dir / key)

    def get_manifest(self, key: str) -> Manifest | None:
        try:
            return Manifest.load(self.runs_dir / key)
        except FileNotFoundError:
            return None

    def summary(self, key: str, *, human_stages: frozenset[Stage] | None = None) -> RunSummary:
        st = self.get_state(key)
        source_admitted = any(
            event.stage is Stage.TASK_MATRIX and event.verdict == "selected"
            for event in st.history
        )
        # A source rejected before TASK MATRIX admitted a candidate has no task, sweep, live cell,
        # or useful manifest-derived card metadata. Avoid opening its manifest/jobs/drive files on
        # every fleet poll. In a 421-source farm this removes ~75% of the summary read amplification.
        screened_out = st.status == "dropped" and not source_admitted
        if screened_out:
            return RunSummary(
                key=key, run_id=st.run_id, slug=st.slug, stage=st.current_stage.value,
                status=st.status, paused=st.paused, harden=st.harden, revise=st.revise,
                progress=self._progress(st), screened_out=True, source_admitted=False,
            )

        man = self.get_manifest(key)
        pa = full_band = None
        if man:
            diff = man.sweeps.get("difficulty") or {}
            # the orchestrator records the real value under `pass_at_1` (legacy key kept as a fallback)
            val = diff.get("pass_at_1", diff.get("claude_code_pass_at_1"))
            pa = str(val) if val is not None else (diff.get("status") or None)
            full = man.sweeps.get("full") or {}
            fams = full.get("families") or {}
            if fams:
                # N-family band string, generic over whatever harnesses this run configured.
                full_band = " ".join(f"{_fam_abbrev(k)}={_fmt_pa(v)}" for k, v in sorted(fams.items()))
            else:
                cc, cx = full.get("claude_code"), full.get("codex")   # pre-families manifests
                if cc is not None or cx is not None:
                    full_band = f"cc={_fmt_pa(cc)} cx={_fmt_pa(cx)}"
        run_dir = self.runs_dir / key
        halt_kind, halt_reason = self._operational_halt(run_dir)
        draft_complete = halt_kind == "draft"
        aj = active_job(run_dir)
        reason_l = (halt_reason or "").lower()
        # DONE = accepted at QA_GATE and exported to the outbox (ADR-0039 — the pipeline's OUTPUT).
        # A finished run must never wear a stale drive.json "blocked" badge.
        exported = st.status == "done"
        # MODE-AWARE human wait (ADR-0039): a run at TASK_MATRIX/QA_GATE only truly awaits a human when
        # that gate is in HUMAN mode. In auto mode (the default) the driver decides itself, so the run
        # must NOT wear a "human" chip — otherwise a whole auto farm reads as "waiting for you".
        if human_stages is None:
            human_stages = _active_human_stages()
        awaiting_human = st.current_stage in human_stages and st.status == "in_progress"
        # "blocked" = genuinely stuck (needs a human/env/spend/missing-input decision). NOT blocked:
        #   * a running agent job (WORKING — surfaced as active_job);
        #   * POLLING a launched sweep OR the `--run-analysis` phase, or within a bounded
        #     auto-retry/backoff (WAITING — it advances itself);
        #   * exported (finished, above).
        op = halt_kind == "blocked" and not aj and not awaiting_human and not exported
        waiting = op and (self._waiting_on_sweep(st, man) or self._benign_halt(reason_l))
        blocked = op and not waiting
        return RunSummary(
            key=key, run_id=st.run_id, slug=st.slug, stage=st.current_stage.value,
            status="draft" if draft_complete else st.status,
            paused=st.paused, harden=st.harden, revise=st.revise,
            difficulty_pass_at_1=pa, full_sweep_band=full_band,
            progress=1.0 if draft_complete else self._progress(st),
            awaiting_human=awaiting_human, active_job=aj,
            blocked=blocked, waiting=waiting, halt_reason=halt_reason,
            exported=exported, ready_for_pr=exported,   # ready_for_pr = deprecated alias (see model)
            created_by=(man.created_by if man else None),
            screened_out=screened_out,
            source_admitted=source_admitted,
        )

    @staticmethod
    def _waiting_on_sweep(st: RunState, man: Manifest | None) -> bool:
        """True iff the run is halted ONLY because it's polling a launched sweep (status
        'running') or is within its bounded auto-retry (status 'errored', attempts < bound) — a benign
        in-flight wait, not a genuine block. Mirrors the orchestrator's sweep state machine."""
        if man is None:
            return False
        key = {Stage.DIFFICULTY_SWEEP: "difficulty", Stage.FULL_SWEEP: "full",
               Stage.QA_PROBE: "qa_probe"}.get(st.current_stage)
        if not key:
            return False
        entry = (man.sweeps or {}).get(key) or {}
        status = entry.get("status")
        if status == "running":
            return True
        if status == "errored":  # still within the bounded auto-retry → it will re-launch itself
            from ..orchestrator import _LAUNCH_MAX_ATTEMPTS, _SWEEP_MAX_ATTEMPTS
            bound = _LAUNCH_MAX_ATTEMPTS if entry.get("experiment") is None else _SWEEP_MAX_ATTEMPTS
            return entry.get("attempts", 0) < bound
        return False

    # Halt-reason signatures that are a benign IN-FLIGHT wait (the run advances itself), not a genuine
    # block — polling a sweep / the QA analysis phase, a running agent, a bounded auto-retry/backoff, OR
    # an agentic cell QUEUED on the shared-credential throttle guard (it launches as a slot frees).
    # A stage-agnostic backstop to `_waiting_on_sweep` (which only knows the sweep stages):
    # e.g. FULL_SWEEP polling `--run-analysis`, a synthesize agent still running, or (under a big farm)
    # create-fill/oracle waiting for an agent slot — the latter is a THROUGHPUT wait, not a failure,
    # and must not read as a red "blocked" (it would make a 50-task farm look broken when it's just busy).
    _BENIGN_HALT_SIGNS = ("polling", "still running", "running on", "agent running", "in background",
                          "backing off", "will retry", "auto-retry", "analysis",
                          "queued", "at capacity", "capacity/cooling", "cooling", "agent slot")

    @classmethod
    def _benign_halt(cls, reason_l: str) -> bool:
        return any(s in reason_l for s in cls._BENIGN_HALT_SIGNS)

    @staticmethod
    def _progress(st: RunState) -> float:
        """Fraction of the forward DAG the run is at. The CURRENT stage is authoritative whenever the
        run sits ON the forward chain — a run routed backward (a revise edge re-entering an earlier
        stage) must show its actual position, not a stale high-water mark (the pb10 confusion: runs
        rewound to ORACLE_GOLDEN still read 45% because history had touched SANITY). Only when the
        current stage is OFF the chain (SYNTHESIZE's side loop, or a terminal like DROPPED) does the
        furthest forward stage from history stand in, so a patching/dropped run still shows the real
        distance travelled instead of collapsing to 0%. `done` is a full bar."""
        if st.status == "done":
            return 1.0
        order = [s.value for s in FORWARD]
        if st.current_stage.value in order:
            idx = order.index(st.current_stage.value)
        else:
            idx = -1
            for ev in st.history:
                for v in (ev.stage.value, ev.next.value):
                    if v in order:
                        idx = max(idx, order.index(v))
        return 0.0 if idx < 0 else round((idx + 1) / len(order), 4)

    @staticmethod
    def _operational_halt(run_dir: Path) -> tuple[str | None, str | None]:
        """Read the last drive halt kind + reason from drive.json.

        The FSM intentionally remains at DIFFICULTY_SWEEP for a draft so it can later resume as a
        full run. The read layer turns the recorded ``halted=draft`` into an honest completed-draft
        presentation without corrupting that resumable state.
        """
        import json

        from ..statestore import store_for
        store, key = store_for(run_dir)
        try:
            raw = store.read(f"{key}/drive.json")
        except OSError:
            return None, None
        if not raw:
            return None, None
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        return d.get("halted"), d.get("halt_reason")

    def list_summaries(self) -> list[RunSummary]:
        # Config-backed human-gate mode is fleet-wide. Resolve it once, not once per run.
        human_stages = _active_human_stages()
        return [self.summary(d.name, human_stages=human_stages) for d in self._run_dirs()]

    def fleet_counters(self, summaries: list[RunSummary] | None = None) -> dict[str, int]:
        # "easy" = the EASY_SHELF terminal (ADR-0040): a good-but-easy task kept on the shelf — its
        # own bucket, neither an accept nor a drop (the shelf is a product output, not a failure).
        c = {"total": 0, "sourced": 0, "screening": 0, "admitted": 0,
             "in_progress": 0, "drafts": 0, "accepted": 0, "exported": 0, "easy": 0,
             "screened_out": 0, "dropped": 0, "blocked": 0, "paused": 0}
        # The fleet endpoint already builds every summary for its response. Reuse that snapshot
        # instead of reading all state/manifest/job files a second time on every UI poll.
        for s in summaries if summaries is not None else self.list_summaries():
            c["total"] += 1
            c["sourced"] += 1
            c["paused"] += int(s.paused)
            c["admitted"] += int(s.source_admitted)
            if not s.source_admitted and not s.screened_out:
                c["screening"] += 1
            if s.status == "done":
                c["accepted"] += 1
                c["exported"] += 1
            elif s.status == "draft":
                c["drafts"] += 1
                c["exported"] += 1
            elif s.status == "easy":
                c["easy"] += 1
            elif s.screened_out:
                c["screened_out"] += 1
            elif s.status == "dropped":
                c["dropped"] += 1
            elif s.status == "blocked":
                c["blocked"] += 1
            else:
                c["in_progress"] += 1
        return c

    def node_statuses(self, st: RunState, *, draft_complete: bool = False) -> dict[str, str]:
        """Per-stage status for the run view: done | current | pending.

        "done" means completed in the run's CURRENT forward pass — strictly upstream of where it is
        now — NOT "ever visited". A harden/revise loops the run BACK and `_finalize_patch` invalidates
        the downstream sweeps, so a stage the run reached in an EARLIER generation but now sits upstream
        of is `pending` again, not `done`. (Without this the DAG shows e.g. Full Sweep "done" while the
        run is back at Difficulty Sweep with no full-sweep result on record — the reported confusion.)"""
        idx = {s.value: i for i, s in enumerate(FORWARD)}
        if draft_complete:
            static_idx = idx[Stage.STATIC_CI.value]
            return {
                stage.value: (
                    "done" if stage is not Stage.SYNTHESIZE and idx.get(stage.value, 10_000) <= static_idx
                    else "pending"
                )
                for stage in FORWARD + [Stage.SYNTHESIZE]
            }
        cur = st.current_stage
        if cur.value in idx:                                # active forward stage
            frontier = idx[cur.value]
        elif cur is Stage.DONE:
            frontier = len(FORWARD)
        else:                                               # SYNTHESIZE (mid-loop) / BLOCKED / DROPPED:
            # done up to the furthest forward stage the run actually REACHED this generation (the stage
            # that triggered the harden/revise, or where it blocked) — NOT the synthesize rejoin point.
            # A run patching after CALIBRATE shows done-through-CALIBRATE + SYNTHESIZE current, instead of
            # appearing to regress to SANITY (the rejoin) — the reported "on Sanity but synthesizing" bug.
            fwd = [idx[ev.stage.value] for ev in st.history if ev.stage.value in idx]
            frontier = (fwd[-1] + 1) if fwd else 0          # [-1] = current generation, not max-ever
        out: dict[str, str] = {}
        for stg in FORWARD + [Stage.SYNTHESIZE]:
            v = stg.value
            if v == cur.value:
                out[v] = "current"
            elif stg is Stage.SYNTHESIZE:                   # off-path loop node: done once it's been used
                out[v] = "done" if (st.harden + st.revise + st.ease) > 0 else "pending"
            else:
                out[v] = "done" if idx[v] < frontier else "pending"
        return out

    def set_paused(self, key: str, paused: bool) -> RunState:
        st = self.get_state(key)
        st.resume() if not paused else st.pause()
        st.save(self.runs_dir / key)
        return st
