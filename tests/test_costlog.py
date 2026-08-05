import json

from programsmith.costlog import cost_context, dashboard, record_envelope


def test_cost_dashboard_records_provider_reported_envelope(tmp_path):
    run = tmp_path / "runs" / "demo"
    envelope = json.dumps({
        "type": "result", "session_id": "s1", "total_cost_usd": 1.25,
        "duration_ms": 3600,
        "usage": {"input_tokens": 100, "output_tokens": 20,
                  "cache_read_input_tokens": 30, "cache_creation_input_tokens": 4},
    })
    with cost_context(run, "TASK_MATRIX"):
        record_envelope(envelope, model="claude-opus-4-8")
        record_envelope(envelope, model="claude-opus-4-8")  # session-id dedupe
    data = dashboard(tmp_path / "runs")
    assert data["totals"]["usd"] == 1.25
    assert data["totals"]["sessions"] == 1
    assert data["by_run"][0]["run"] == "demo"
