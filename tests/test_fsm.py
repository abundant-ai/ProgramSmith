"""Tests for the deterministic FSM router + RunState persistence (invariant #1).

Table-driven over the ADR-0040 routing: the smoke/frontier tuning budgets, the EASY_SHELF
terminal, the legacy QA_ON_GPT/PR drains, the probe-exhausted-BLOCKS vs smoke-exhausted-PROCEEDS
asymmetry, and crash/resume. The router routes only on verdicts; counters bound every loop.
"""

import pytest

from programsmith.fsm import (
    FRONTIER_TUNE_MAX,
    HARDEN_MAX,
    REVISE_MAX,
    SMOKE_TUNE_MAX,
    TERMINAL,
    Counters,
    FsmError,
    Stage,
    route,
)
from programsmith.state import RunState

# ---- forward chain + terminals (table-driven) ------------------------------------------

HAPPY = [
    (Stage.INGEST_LOCK, "pass", Stage.TASK_MATRIX),
    (Stage.TASK_MATRIX, "selected", Stage.ORACLE_GOLDEN),
    (Stage.ORACLE_GOLDEN, "pass", Stage.CREATE),
    (Stage.CREATE, "pass", Stage.SANITY),
    (Stage.SANITY, "pass", Stage.STATIC_CI),
    (Stage.STATIC_CI, "pass", Stage.DIFFICULTY_SWEEP),
    (Stage.DIFFICULTY_SWEEP, "done", Stage.CALIBRATE),
    (Stage.CALIBRATE, "proceed", Stage.QA_PROBE),
    (Stage.QA_PROBE, "clean", Stage.FULL_SWEEP),
    (Stage.FULL_SWEEP, "done", Stage.QA_GATE),        # no QA_ON_GPT hop anymore (ADR-0039)
    (Stage.QA_GATE, "accept", Stage.DONE),            # no PR hop — accept exports + completes
]


@pytest.mark.parametrize("stage,verdict,expected", HAPPY)
def test_happy_edge(stage, verdict, expected):
    assert route(stage, verdict, Counters()).next is expected


def test_happy_path_reaches_done_via_runstate():
    s = RunState.start("run-1", "id-abc", "difft")
    for _stage, verdict, expected in HAPPY:
        dec = s.advance(verdict)
        assert dec.next is expected and s.current_stage is expected
    assert s.status == "done" and s.terminal


EDGES = [
    # (stage, verdict, counters, expected next)
    (Stage.INGEST_LOCK, "fail", Counters(), Stage.DROPPED),
    (Stage.TASK_MATRIX, "none_selected", Counters(), Stage.DROPPED),
    (Stage.ORACLE_GOLDEN, "fail", Counters(), Stage.BLOCKED),
    (Stage.CREATE, "fail", Counters(), Stage.BLOCKED),
    (Stage.SANITY, "fail", Counters(), Stage.SYNTHESIZE),
    (Stage.STATIC_CI, "fail", Counters(), Stage.SYNTHESIZE),
    # CALIBRATE (smoke decision): tune edges bounded by SMOKE_TUNE_MAX; flag_broken drops
    (Stage.CALIBRATE, "harden", Counters(), Stage.SYNTHESIZE),
    (Stage.CALIBRATE, "ease", Counters(), Stage.SYNTHESIZE),
    (Stage.CALIBRATE, "flag_broken", Counters(), Stage.DROPPED),
    (Stage.CALIBRATE, "harden", Counters(smoke_tunes=SMOKE_TUNE_MAX), Stage.QA_PROBE),  # exhausted → PROCEED
    (Stage.CALIBRATE, "ease", Counters(smoke_tunes=SMOKE_TUNE_MAX), Stage.QA_PROBE),
    # QA_PROBE: an exploit shares the smoke budget but BLOCKS on exhaustion (never waved through)
    (Stage.QA_PROBE, "clean", Counters(), Stage.FULL_SWEEP),
    (Stage.QA_PROBE, "harden", Counters(), Stage.SYNTHESIZE),
    (Stage.QA_PROBE, "harden", Counters(smoke_tunes=SMOKE_TUNE_MAX), Stage.BLOCKED),
    # FULL_SWEEP (frontier decision): bounded tunes; shelve/flag_broken/revise edges
    (Stage.FULL_SWEEP, "harden", Counters(), Stage.SYNTHESIZE),
    (Stage.FULL_SWEEP, "ease", Counters(), Stage.SYNTHESIZE),
    (Stage.FULL_SWEEP, "harden", Counters(frontier_tunes=FRONTIER_TUNE_MAX), Stage.EASY_SHELF),
    (Stage.FULL_SWEEP, "ease", Counters(frontier_tunes=FRONTIER_TUNE_MAX), Stage.DROPPED),
    (Stage.FULL_SWEEP, "shelve", Counters(), Stage.EASY_SHELF),
    (Stage.FULL_SWEEP, "flag_broken", Counters(), Stage.DROPPED),
    (Stage.FULL_SWEEP, "revise", Counters(), Stage.SYNTHESIZE),
    (Stage.FULL_SWEEP, "revise", Counters(revise=REVISE_MAX), Stage.BLOCKED),
    # QA_GATE
    (Stage.QA_GATE, "reject", Counters(), Stage.DROPPED),
    (Stage.QA_GATE, "revise", Counters(), Stage.SYNTHESIZE),
    (Stage.QA_GATE, "revise", Counters(revise=REVISE_MAX), Stage.BLOCKED),
    # LEGACY drains (ADR-0039): both verdicts of QA_ON_GPT route FORWARD; PR completes, opens nothing
    (Stage.QA_ON_GPT, "done", Counters(), Stage.QA_GATE),
    (Stage.QA_ON_GPT, "revise", Counters(), Stage.QA_GATE),   # revise no longer loops — drains
    (Stage.PR, "done", Counters(), Stage.DONE),
]


@pytest.mark.parametrize("stage,verdict,counters,expected", EDGES)
def test_edge(stage, verdict, counters, expected):
    assert route(stage, verdict, counters).next is expected


def test_terminals_and_bounds_constants():
    assert TERMINAL == {Stage.DONE, Stage.DROPPED, Stage.BLOCKED, Stage.EASY_SHELF}
    assert SMOKE_TUNE_MAX == 2 and FRONTIER_TUNE_MAX == 1 and REVISE_MAX == 2
    assert HARDEN_MAX == SMOKE_TUNE_MAX + FRONTIER_TUNE_MAX  # deprecated alias stays meaningful


# ---- counter semantics -----------------------------------------------------------------

def test_smoke_tune_bumps_move_counter_and_shared_budget():
    d = route(Stage.CALIBRATE, "harden", Counters())
    assert d.counters.harden == 1 and d.counters.ease == 0 and d.counters.smoke_tunes == 1
    d = route(Stage.CALIBRATE, "ease", d.counters)
    assert d.counters.harden == 1 and d.counters.ease == 1 and d.counters.smoke_tunes == 2
    # budget now exhausted: the NEXT smoke tune proceeds forward instead (measure, don't predict)
    d = route(Stage.CALIBRATE, "harden", d.counters)
    assert d.next is Stage.QA_PROBE and "measure" in d.reason


def test_frontier_tune_budget_and_offramps():
    d = route(Stage.FULL_SWEEP, "harden", Counters())
    assert d.next is Stage.SYNTHESIZE and d.counters.frontier_tunes == 1
    assert d.synthesize_rejoin is Stage.STATIC_CI
    # exhausted harden → shelved as easy (kept!); exhausted ease → dropped (defect persists)
    assert route(Stage.FULL_SWEEP, "harden", d.counters).next is Stage.EASY_SHELF
    assert route(Stage.FULL_SWEEP, "ease", d.counters).next is Stage.DROPPED


def test_probe_exploit_never_waved_through():
    """Asymmetry lock: saturation tuning may PROCEED on an exhausted budget, but a probe-discovered
    exploit must BLOCK — a known reward-hack is never shipped to the frontier."""
    exhausted = Counters(smoke_tunes=SMOKE_TUNE_MAX)
    assert route(Stage.CALIBRATE, "harden", exhausted).next is Stage.QA_PROBE
    d = route(Stage.QA_PROBE, "harden", exhausted)
    assert d.next is Stage.BLOCKED and "exploit" in d.reason


def test_revise_counts_separately_from_tunes():
    d = route(Stage.FULL_SWEEP, "revise", Counters(smoke_tunes=2, frontier_tunes=1))
    assert d.next is Stage.SYNTHESIZE                      # revise budget is its own lane
    assert d.counters.revise == 1 and d.counters.smoke_tunes == 2
    assert d.synthesize_rejoin is Stage.SANITY             # env fixes re-validate from SANITY


def test_route_bounds_overridable_via_kwargs():
    # config can thread per-run bounds; a raised smoke budget keeps tuning past the default
    d = route(Stage.CALIBRATE, "harden", Counters(smoke_tunes=2), smoke_tune_max=3)
    assert d.next is Stage.SYNTHESIZE and d.counters.smoke_tunes == 3
    d = route(Stage.FULL_SWEEP, "harden", Counters(frontier_tunes=1), frontier_tune_max=2)
    assert d.next is Stage.SYNTHESIZE


# ---- synthesize rejoin + detail threading ----------------------------------------------

def test_sanity_fail_rejoins_sanity_and_detail_enriches():
    s = RunState.start("run-s", "id")
    for v in ("pass", "selected", "pass", "pass"):
        s.advance(v)
    s.advance("fail", detail="sanity failed: ['produced_owned_by_nobody']")
    ev = s.history[-1]
    assert s.current_stage is Stage.SYNTHESIZE and s.synthesize_rejoin is Stage.SANITY
    assert "produced_owned_by_nobody" in ev.reason and "revise 1/2" in ev.reason
    assert s.advance("done").next is Stage.SANITY
    assert s.synthesize_rejoin is None


def test_ease_edge_rejoins_static_ci():
    s = RunState.start("run-e", "id")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done"):
        s.advance(v)
    assert s.current_stage is Stage.CALIBRATE
    s.advance("ease")
    assert s.current_stage is Stage.SYNTHESIZE and s.ease == 1 and s.smoke_tunes == 1
    assert s.advance("done").next is Stage.STATIC_CI


def test_full_sweep_shelve_terminal_status_easy():
    s = RunState.start("run-shelf", "id")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"):
        s.advance(v)
    s.advance("shelve")
    assert s.current_stage is Stage.EASY_SHELF and s.terminal and s.status == "easy"


def test_legacy_qa_on_gpt_drains_forward_from_persisted_state():
    """A pre-ADR-0039 run parked at QA_ON_GPT (or PR) still loads and drains to DONE."""
    s = RunState.start("legacy", "id")
    s.current_stage = Stage.QA_ON_GPT
    assert s.advance("done").next is Stage.QA_GATE
    assert s.advance("accept").next is Stage.DONE


def test_bad_verdict_and_terminal_rejected():
    with pytest.raises(FsmError):
        route(Stage.INGEST_LOCK, "proceed", Counters())
    with pytest.raises(FsmError):
        route(Stage.CALIBRATE, "shelve", Counters())     # shelve is a FULL_SWEEP-only verdict
    for t in TERMINAL:
        with pytest.raises(FsmError):
            route(t, "done", Counters())


# ---- persistence / resume ---------------------------------------------------------------

def test_resume_roundtrip_carries_new_counters(tmp_path):
    s = RunState.start("run-resume", "id-xyz", "difft", ts="2026-07-06T10:00:00Z")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done"):
        s.advance(v, ts="t")
    s.advance("ease", ts="t")     # smoke tune → SYNTHESIZE
    s.save(tmp_path)
    r = RunState.load(tmp_path)
    assert r.current_stage is Stage.SYNTHESIZE
    assert r.ease == 1 and r.smoke_tunes == 1 and r.frontier_tunes == 0
    assert r.advance("done").next is Stage.STATIC_CI     # continue from the resumed state


def test_reopen_allows_easy_shelf(tmp_path):
    """A shelved task is a prime reopen candidate after a hardening idea: reopen resets ALL loop
    counters (incl. ease/tunes) and re-enters the tune loop at SYNTHESIZE."""
    s = RunState.start("run-shelf2", "id")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean", "shelve"):
        s.advance(v)
    assert s.current_stage is Stage.EASY_SHELF
    s.reopen_for_harden()
    assert s.current_stage is Stage.SYNTHESIZE and s.status == "in_progress"
    assert (s.harden, s.revise, s.ease, s.smoke_tunes, s.frontier_tunes) == (0, 0, 0, 0, 0)


def test_smoke_tune_loop_is_bounded_end_to_end():
    """A GLM band that never moves cannot loop forever: each CALIBRATE harden bumps smoke_tunes;
    at the budget the run PROCEEDS to the frontier instead of spinning (or blocking)."""
    s = RunState.start("r", "id")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done"):
        s.advance(v)
    hops = 0
    while s.current_stage is Stage.CALIBRATE and hops < 10:
        dec = s.advance("harden")
        hops += 1
        if dec.next is Stage.QA_PROBE:
            break
        assert dec.next is Stage.SYNTHESIZE
        s.advance("done")        # rejoin STATIC_CI
        s.advance("pass")        # → DIFFICULTY_SWEEP
        s.advance("done")        # → CALIBRATE
    assert s.current_stage is Stage.QA_PROBE and s.smoke_tunes == SMOKE_TUNE_MAX
