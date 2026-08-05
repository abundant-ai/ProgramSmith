"""RunState — the persisted FSM tracker (machine source of truth).

A thin, idempotent wrapper over the pure router in `fsm.py`. Holds the per-run identity, the
current stage, loop counters, the armed SYNTHESIZE rejoin point, and an append-only history of
transitions. Persists to `<run_dir>/state.json`. Crash/resume = load the file and keep going.

The rich per-run *context* (source, dimensions, oracle, sweeps, …) lives in a separate manifest
(see schemas/manifest.schema.json at the repo root); this object is deliberately small and rigid.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .fsm import HUMAN_STAGES, TERMINAL, Counters, Decision, Stage, route
from .statestore import store_for

STATE_FILENAME = "state.json"


def _status_for(stage: Stage) -> str:
    return {
        Stage.DONE: "done",
        Stage.DROPPED: "dropped",
        Stage.BLOCKED: "blocked",
        Stage.EASY_SHELF: "easy",
    }.get(stage, "in_progress")


class StageEvent(BaseModel):
    stage: Stage
    verdict: str
    next: Stage
    reason: str
    ts: str | None = None


class RunState(BaseModel):
    schema_version: str = "0.1.0"
    run_id: str
    task_identity: str
    slug: str | None = None

    current_stage: Stage = Stage.INGEST_LOCK
    status: str = "in_progress"
    paused: bool = False

    harden: int = 0
    revise: int = 0
    ease: int = 0            # ease patches applied (ADR-0040: bidirectional tuning)
    smoke_tunes: int = 0     # harden+ease rounds consumed at the smoke phase (bound SMOKE_TUNE_MAX)
    frontier_tunes: int = 0  # harden+ease rounds consumed at the frontier phase (bound FRONTIER_TUNE_MAX)
    synthesize_rejoin: Stage | None = None  # armed when a backward edge routes into SYNTHESIZE

    history: list[StageEvent] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None

    # ---- transitions -----------------------------------------------------------------

    @property
    def awaiting_human(self) -> bool:
        """True when the run sits at a human-review stage and needs a verdict supplied."""
        return self.current_stage in HUMAN_STAGES and self.status == "in_progress"

    @property
    def terminal(self) -> bool:
        return self.current_stage in TERMINAL

    @property
    def halted(self) -> bool:
        """The runner must not advance into the next stage when halted: terminal, paused
        (operational stop — a halt, not a review), or awaiting a human verdict."""
        return self.terminal or self.paused or self.awaiting_human

    def pause(self) -> None:
        """Operational pause/stop — halts at the next inter-stage checkpoint (not a review prompt)."""
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def reopen_for_harden(self, ts: str | None = None) -> None:
        """Human override: un-terminal a DROPPED/BLOCKED run and re-enter the harden loop with a fresh
        budget. Used when the operator disagrees with an auto-drop / exhausted-bound and wants to try
        more hardening. Records the reopen in history (verdict 'harden' so the SYNTHESIZE trigger reads
        it as a harden patch), resets the loop counters, and re-enters at SYNTHESIZE (rejoin STATIC_CI).
        It will re-measure and, if still saturated past the fresh budget, honestly drop/block again."""
        if self.current_stage not in (Stage.DROPPED, Stage.BLOCKED, Stage.EASY_SHELF):
            raise ValueError(f"run is {self.current_stage.value}, not terminal; nothing to reopen")
        # A reopen re-enters at SYNTHESIZE, which PATCHES an existing task. A run dropped before a task
        # was ever built (e.g. at INGEST/TASK_MATRIX — bad license, no candidate selected) has nothing
        # to patch → SYNTHESIZE crashes on a missing task dir. Refuse: such a source must be RE-INGESTED
        # (fix the license/source), not hardened.
        _post_create = {Stage.SANITY, Stage.STATIC_CI, Stage.DIFFICULTY_SWEEP, Stage.CALIBRATE,
                        Stage.QA_PROBE, Stage.FULL_SWEEP, Stage.QA_ON_GPT, Stage.QA_GATE,
                        Stage.SYNTHESIZE}
        built = any(e.stage in _post_create or (e.stage is Stage.CREATE and e.verdict == "pass")
                    for e in self.history)
        if not built:
            raise ValueError("run was dropped before a task was built (no CREATE) — nothing to harden; "
                             "fix the source/license and create a fresh run instead of re-opening")
        self.history.append(StageEvent(
            stage=self.current_stage, verdict="harden", next=Stage.SYNTHESIZE,
            reason="human re-opened the run — re-entering the harden loop with a fresh budget "
                   "(override of the auto-drop / exhausted bound / easy-shelf)", ts=ts))
        self.harden = 0
        self.revise = 0
        self.ease = 0
        self.smoke_tunes = 0
        self.frontier_tunes = 0
        self.synthesize_rejoin = Stage.STATIC_CI
        self.current_stage = Stage.SYNTHESIZE
        self.status = _status_for(Stage.SYNTHESIZE)
        self.updated_at = ts

    def advance(self, verdict: str, ts: str | None = None, detail: str | None = None) -> Decision:
        """Apply a stage verdict, move the FSM, and record history. Returns the Decision. `detail` is
        the gate handler's reason; it enriches the recorded reason on revise edges (does not route)."""
        if self.terminal:
            raise ValueError(f"run is terminal ({self.current_stage.value}); cannot advance")
        prev = self.current_stage
        decision = route(
            prev, verdict,
            Counters(harden=self.harden, revise=self.revise, ease=self.ease,
                     smoke_tunes=self.smoke_tunes, frontier_tunes=self.frontier_tunes),
            self.synthesize_rejoin, detail=detail,
        )

        self.history.append(
            StageEvent(stage=prev, verdict=verdict, next=decision.next, reason=decision.reason, ts=ts)
        )
        c = decision.counters
        self.harden, self.revise, self.ease = c.harden, c.revise, c.ease
        self.smoke_tunes, self.frontier_tunes = c.smoke_tunes, c.frontier_tunes

        if decision.next is Stage.SYNTHESIZE:
            self.synthesize_rejoin = decision.synthesize_rejoin
        elif prev is Stage.SYNTHESIZE:
            self.synthesize_rejoin = None

        self.current_stage = decision.next
        self.status = _status_for(decision.next)
        self.updated_at = ts
        return decision

    # ---- persistence (idempotent) ----------------------------------------------------

    def save(self, run_dir: str | Path) -> Path:
        # Routed through the StateStore seam: `LocalFileStore` is the atomic write (tmp +
        # os.replace under run_dir); the working tree stays a real FS. Background agentic jobs
        # re-load state concurrently with the driver's save — write_atomic prevents torn reads.
        run_dir = Path(run_dir)
        store, key = store_for(run_dir)
        store.write_atomic(f"{key}/{STATE_FILENAME}", self.model_dump_json(indent=2) + "\n")
        return run_dir / STATE_FILENAME

    @classmethod
    def load(cls, run_dir: str | Path) -> "RunState":
        store, key = store_for(run_dir)
        raw = store.read(f"{key}/{STATE_FILENAME}")
        if raw is None:
            raise FileNotFoundError(str(Path(run_dir) / STATE_FILENAME))
        return cls.model_validate_json(raw)

    @classmethod
    def start(
        cls,
        run_id: str,
        task_identity: str,
        slug: str | None = None,
        ts: str | None = None,
    ) -> "RunState":
        return cls(
            run_id=run_id,
            task_identity=task_identity,
            slug=slug,
            current_stage=Stage.INGEST_LOCK,
            created_at=ts,
            updated_at=ts,
        )
