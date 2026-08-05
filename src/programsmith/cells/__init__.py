"""LLM cells — quarantined synthesis steps that emit schema-validated JSON (invariant #4).

Every cell builds a prompt, calls `llm.run_cell` (subscription `claude -p`, ADR-0015), and returns
a validated pydantic model. Cells never decide flow, compute a reward, or edit a gate; the
orchestrator routes on the gate verdict that consumes a cell's validated output.
"""
