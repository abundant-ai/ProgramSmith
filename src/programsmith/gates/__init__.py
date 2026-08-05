"""Deterministic gates — pure(ish) code nodes that emit a verdict for the FSM.

Each gate reads/extends the manifest and returns a `GateResult(verdict, ...)`. Gates never call
an LLM and never reason; they compute a verdict deterministically. The orchestrator routes on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    verdict: str                      # must be in the stage's ALLOWED_VERDICTS
    reason: str
    detail: dict = field(default_factory=dict)
