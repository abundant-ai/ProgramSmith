"""`programsmith serve` auto-drives real work but requires explicit consent for billable sweeps.

There is no synthetic/simulate path. The two human gates stay intact. These tests pin that contract
at the CLI layer; uvicorn is stubbed so no port is bound.
"""

import os
import sys
import types

from argparse import Namespace
from pathlib import Path

import pytest

from programsmith.cli import _ensure_dashboard, _open_dashboard, _remember_runs_dir, _save_dashboard_state, main
from programsmith.config import LhConfig


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("PROGRAMSMITH_RUNS_DIR", raising=False)


def _stub_uvicorn(monkeypatch) -> dict:
    captured: dict = {}
    fake = types.ModuleType("uvicorn")
    fake.run = lambda *a, **k: captured.update(ran=True, kwargs=k)
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    # sandbox os.environ so the command's direct writes don't leak across tests
    monkeypatch.setattr(os, "environ", os.environ.copy())
    return captured


def test_serve_defaults_autodrive_and_spend_off(monkeypatch, tmp_path):
    captured = _stub_uvicorn(monkeypatch)
    rc = main(["serve", "--foreground", "--runs-dir", str(tmp_path)])
    assert rc == 0 and captured.get("ran")
    assert os.environ["PROGRAMSMITH_AUTODRIVE"] == "1"            # auto-driver on by default
    assert os.environ["PROGRAMSMITH_AUTODRIVE_SPEND"] == "0"      # spending requires explicit consent
    assert os.environ["PROGRAMSMITH_RUNS_DIR"] == str(tmp_path)   # serves & drives the SAME dir
    assert Path(LhConfig.load_persisted().runs_dir) == tmp_path.resolve()
    # no synthetic/simulate plumbing exists anymore
    assert "PROGRAMSMITH_AUTODRIVE_SIMULATE" not in os.environ
    assert "PROGRAMSMITH_AUTODRIVE_AUTO_APPROVE" not in os.environ


def test_serve_output_is_concise(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("programsmith.cli._start_dashboard", lambda *_a, **_k: {
        "url": "http://127.0.0.1:8765",
        "runs_dir": str(tmp_path),
        "autodrive": False,
        "autodrive_interval": 8,
        "spend": False,
        "log_path": str(tmp_path / "dashboard.log"),
        "reused": False,
    })
    main(["serve", "--runs-dir", str(tmp_path), "--no-autodrive"])
    rendered = capsys.readouterr().out
    assert "ProgramSmith dashboard" in rendered
    assert "http://127.0.0.1:8765" in rendered
    assert "Runs" in rendered and "Autodrive" in rendered and "Sweeps" in rendered
    assert "Running in background" in rendered
    assert "programsmith stop" in rendered
    assert "[UI]" not in rendered
    assert "REAL results" not in rendered
    assert "task-generation cells" not in rendered


def test_bare_serve_uses_last_explicit_runs_dir(monkeypatch, tmp_path):
    fleet = tmp_path / "pilot-runs"
    saved = LhConfig(runs_dir=str(fleet))
    saved.save()
    _stub_uvicorn(monkeypatch)

    assert main(["serve", "--foreground", "--no-autodrive"]) == 0
    assert os.environ["PROGRAMSMITH_RUNS_DIR"] == str(fleet.resolve())


def test_explicit_creation_fleet_is_remembered(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    fleet = tmp_path / "created-here"

    assert _remember_runs_dir(Namespace(runs_dir=str(fleet))) == fleet.resolve()
    assert Path(LhConfig.load_persisted().runs_dir) == fleet.resolve()


def test_dashboard_skips_wrong_fleet_on_requested_port(monkeypatch, tmp_path):
    import subprocess

    expected = str(tmp_path.resolve())
    calls: list[tuple[list[str], dict]] = []

    def health(_host, port, timeout=0.75):
        if port == 8765:
            return {"ok": True, "frontend_built": True, "runs_dir": "/some/other/fleet"}
        if port == 8766 and calls:
            return {"ok": True, "frontend_built": True, "runs_dir": expected,
                    "pid": 123, "instance_id": "test-token"}
        return None

    class Proc:
        pid = 123

        @staticmethod
        def poll():
            return None

    def popen(argv, **_kwargs):
        calls.append((argv, _kwargs))
        return Proc()

    monkeypatch.setattr("programsmith.cli._dashboard_health", health)
    monkeypatch.setattr("programsmith.cli._port_is_open", lambda _host, port: port == 8765)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "test-token")

    assert _ensure_dashboard(tmp_path, "127.0.0.1", 8765) == "http://127.0.0.1:8766"
    argv, kwargs = calls[0]
    assert argv[argv.index("--port") + 1] == "8766"
    assert "--foreground" in argv
    assert kwargs["start_new_session"] is True


def test_open_dashboard_is_best_effort_and_disabled_in_ci(monkeypatch):
    calls = []
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PROGRAMSMITH_NO_BROWSER", raising=False)
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: calls.append((url, new)) or True)
    assert _open_dashboard("http://127.0.0.1:8765/run/demo") is True
    assert calls == [("http://127.0.0.1:8765/run/demo", 2)]

    monkeypatch.setenv("CI", "1")
    assert _open_dashboard("http://127.0.0.1:8765/run/demo") is False
    assert len(calls) == 1


def test_serve_no_autodrive_opts_out(monkeypatch, tmp_path):
    _stub_uvicorn(monkeypatch)
    main(["serve", "--foreground", "--runs-dir", str(tmp_path), "--no-autodrive"])
    assert "PROGRAMSMITH_AUTODRIVE" not in os.environ             # disabled on request


def test_serve_spend_explicitly_authorizes_billable_stages(monkeypatch, tmp_path):
    _stub_uvicorn(monkeypatch)
    main(["serve", "--foreground", "--runs-dir", str(tmp_path), "--spend"])
    assert os.environ["PROGRAMSMITH_AUTODRIVE"] == "1"
    assert os.environ["PROGRAMSMITH_AUTODRIVE_SPEND"] == "1"


def test_serve_rejects_non_loopback_bind(monkeypatch, tmp_path, capsys):
    called = False

    def start(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("programsmith.cli._start_dashboard", start)
    assert main(["serve", "--host", "0.0.0.0", "--runs-dir", str(tmp_path)]) == 2
    assert called is False
    rendered = capsys.readouterr().out
    assert "Refusing" in rendered and "SSH tunnel" in rendered


def test_stop_terminates_the_recorded_dashboard(monkeypatch, tmp_path, capsys):
    state = {
        "url": "http://127.0.0.1:8765",
        "host": "127.0.0.1",
        "port": 8765,
        "pid": 4321,
        "token": "owned-instance",
        "runs_dir": str(tmp_path),
    }
    _save_dashboard_state(state)
    monkeypatch.setattr("programsmith.cli._dashboard_health", lambda *_a, **_k: {
        "ok": True, "frontend_built": True, "pid": 4321, "instance_id": "owned-instance",
    })
    alive = iter([True, False])
    monkeypatch.setattr("programsmith.cli._pid_is_alive", lambda _pid: next(alive))
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert main(["stop"]) == 0
    assert signals and signals[0][0] == 4321
    assert not (tmp_path / "dashboard.json").exists()
    assert "Stopped" in capsys.readouterr().out
