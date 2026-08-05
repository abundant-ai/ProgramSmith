"""WORKFLOW ANALYSIS data layer — append a structured record for every backward move.

Off the per-task critical path (PRODUCT.md workflow_analysis): each harden / revise / drop appends
a YAML block to WORKFLOW_NOTES.md. An offline LLM later mines these for pipeline/template/catalog
improvements (the mining loop is deferred for v1 — this is just the collection). `trigger` quotes
the deterministic gate verdict that routed the move (gates decide; cells only annotate).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from .fsm import Stage

# moves whose `next` stage indicates a backward/off-ramp edge worth logging. EASY_SHELF counts
# (ADR-0040): a shelving is exactly the kind of outcome the offline mining loop should see — which
# sources saturate the frontier tells the ideas backlog what NOT to ingest next.
_BACKWARD_NEXT = {Stage.SYNTHESIZE, Stage.DROPPED, Stage.EASY_SHELF}


class BackwardMove(BaseModel):
    run_id: str
    task_identity: str
    ts: str | None = None
    move: str                       # harden | ease | revise | drop | shelve
    from_stage: str
    to_stage: str
    trigger: str                    # quotes the deterministic verdict that routed here
    what_failed: str = ""
    what_changed: str = ""          # the surgical patch (or "n/a — dropped"/"n/a — shelved")
    loop_index: int = 0             # which harden/ease/revise iteration
    catalog_delta: str | None = None  # new reward-hack-catalog entry id, if any

    def to_yaml_block(self) -> str:
        lines = [
            f"- run_id: {self.run_id}",
            f"  task_identity: {self.task_identity}",
            f"  ts: {self.ts or 'null'}",
            f"  move: {self.move}",
            f"  from_stage: {self.from_stage}",
            f"  to_stage: {self.to_stage}",
            f"  trigger: {self.trigger!r}",
            f"  what_failed: {self.what_failed!r}",
            f"  what_changed: {self.what_changed!r}",
            f"  loop_index: {self.loop_index}",
            f"  catalog_delta: {self.catalog_delta or 'null'}",
        ]
        return "\n".join(lines) + "\n"


def append_move(move: BackwardMove, notes_path: str | Path) -> None:
    """Append one record to WORKFLOW_NOTES.md (creates a minimal file if absent)."""
    path = Path(notes_path)
    if not path.exists():
        path.write_text("# WORKFLOW_NOTES.md — backward-move log\n\n## Log\n")
    with path.open("a") as f:
        f.write(move.to_yaml_block())


def record_backward_move(
    state,
    decision,
    *,
    trigger: str,
    notes_path: str | Path,
    what_failed: str = "",
    what_changed: str = "",
    catalog_delta: str | None = None,
    ts: str | None = None,
) -> BackwardMove | None:
    """Build + append a record IFF `decision` is a backward/off-ramp move (→ SYNTHESIZE, → DROPPED,
    or → EASY_SHELF). Returns the record (or None if it wasn't one). The move kind is read from the
    routed VERDICT when history carries it (record is called after state.advance, so the last event
    is this transition — deterministic, no text sniffing); the trigger-text heuristic remains the
    fallback for callers that append no history (legacy tests / manual notes)."""
    if decision.next not in _BACKWARD_NEXT:
        return None
    if decision.next is Stage.DROPPED:
        move, loop_index, what_changed = "drop", 0, what_changed or "n/a — dropped"
    elif decision.next is Stage.EASY_SHELF:
        move, loop_index = "shelve", 0
        what_changed = what_changed or "n/a — shelved (kept on the easy shelf)"
    else:
        verdict = state.history[-1].verdict if state.history else ""
        if verdict in ("harden", "ease", "revise"):
            move = verdict
        elif verdict == "fail":
            move = "revise"                                     # SANITY/STATIC fail edges
        else:                                                   # no usable verdict → old heuristic
            move = "revise" if state.revise and "revise" in trigger.lower() else "harden"
        loop_index = {"revise": state.revise, "ease": state.ease}.get(move, state.harden)
    rec = BackwardMove(
        run_id=state.run_id, task_identity=state.task_identity, ts=ts or state.updated_at,
        move=move, from_stage=state.history[-1].stage.value if state.history else "?",
        to_stage=decision.next.value, trigger=trigger,
        what_failed=what_failed, what_changed=what_changed,
        loop_index=loop_index, catalog_delta=catalog_delta,
    )
    append_move(rec, notes_path)
    return rec
