import json

import httpx

from programsmith import oddish
from programsmith.oddish import load_state, refresh_state, save_state, submit_task, task_content_hash


def test_task_content_hash_is_stable_and_content_sensitive(tmp_path):
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text("version = 1\n")
    (task / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")

    first = task_content_hash(task)
    assert first == task_content_hash(task)
    (task / "task.toml").write_text("version = 2\n")
    assert task_content_hash(task) != first


def test_task_content_hash_records_symlink_target_without_following_it(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (task / "link").symlink_to(outside)
    first = task_content_hash(task)
    outside.write_text("changed")
    assert task_content_hash(task) == first
    (task / "link").unlink()
    (task / "link").symlink_to("elsewhere")
    assert task_content_hash(task) != first


def test_oddish_state_is_atomic_and_contains_no_credential(tmp_path):
    saved = save_state(tmp_path, {"status": "queued", "task_id": "t1", "trials": []})
    assert saved["updated_at"]
    assert load_state(tmp_path)["task_id"] == "t1"
    raw = json.loads((tmp_path / "oddish.json").read_text())
    assert "api_key" not in raw


def test_submit_task_uses_low_priority_public_single_trial(tmp_path, monkeypatch):
    task = tmp_path / "exported" / "demo-task"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("version = 1\n")
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers") or {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs, self.headers))
            request = httpx.Request(method, url)
            if url.endswith("/tasks/sweep"):
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "id": "task-1",
                        "experiment_id": "exp-1",
                        "experiment_name": "ProgramSmith demo",
                        "new_trial_ids": ["trial-1"],
                    },
                )
            if url.endswith("/experiments/exp-1/share"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"public_token": "share-1", "is_public": True},
                )
            raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(oddish.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        oddish,
        "_upload_task",
        lambda *_args, **_kwargs: {
            "task_id": "task-1",
            "existing_task": False,
            "content_hash": "hash-1",
        },
    )

    result = submit_task(
        tmp_path / "run",
        task,
        api_key="not-written-to-disk",
        api_url="https://api.oddish.test",
        dashboard_url="https://oddish.test",
        agent="claude-code",
        model="anthropic/claude-sonnet-4-6",
    )

    assert result["status"] == "queued"
    assert result["public_url"] == "https://oddish.test/share/share-1"
    sweep = next(item for item in calls if item[1].endswith("/tasks/sweep"))
    assert sweep[2]["json"]["priority"] == "low"
    assert sweep[2]["json"]["publish_experiment"] is True
    assert sweep[2]["json"]["configs"] == [{
        "agent": "claude-code",
        "model": "anthropic/claude-sonnet-4-6",
        "n_trials": 1,
    }]
    assert sweep[2]["headers"]["Idempotency-Key"]
    assert "api_key" not in (tmp_path / "run" / "oddish.json").read_text()


def test_refresh_state_compacts_public_trial_and_error_message(tmp_path, monkeypatch):
    save_state(tmp_path, {
        "status": "queued",
        "task_id": "task-1",
        "public_token": "share-1",
        "trials": [],
    })
    monkeypatch.setattr(oddish, "_public_get", lambda *_args, **_kwargs: [{
        "id": "trial-1",
        "status": "success",
        "agent": "claude-code",
        "model": "anthropic/claude-sonnet-4-6",
        "reward": 0.75,
        "error_message": "verifier note",
        "trajectory_duration_seconds": 14.2,
        "total_tool_calls": 3,
    }])

    result = refresh_state(tmp_path, api_url="https://api.oddish.test")
    assert result["status"] == "complete"
    assert result["trials"] == [{
        "id": "trial-1",
        "index": None,
        "status": "success",
        "agent": "claude-code",
        "model": "anthropic/claude-sonnet-4-6",
        "reward": 0.75,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": 14.2,
        "tool_calls": 3,
        "cost_usd": None,
        "error": "verifier note",
    }]
