"""Small, dependency-light bridge from an exported ProgramSmith task to hosted Oddish.

The hosted Oddish API already owns task storage, sandbox execution, trajectories, and public
experiment pages. ProgramSmith only performs the client-side handoff:

1. archive and upload one exported Harbor task;
2. submit one agent trial and request a public experiment;
3. persist the returned identifiers beside the local run; and
4. proxy a compact public status/trajectory view for the local dashboard.

The Oddish API key is read from :class:`programsmith.config.LhConfig`; it is never written into the
per-run state file or returned to the browser.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


STATE_FILE = "oddish.json"
_TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
_TERMINAL = {"success", "failed", "cancelled", "canceled", "skipped", "error"}


class OddishError(RuntimeError):
    """A concise, user-actionable Oddish handoff error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / STATE_FILE


def load_state(run_dir: str | Path) -> dict[str, Any]:
    path = _state_path(run_dir)
    if not path.is_file():
        return {"status": "idle", "trials": []}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "failed", "error": "Oddish state is unreadable", "trials": []}
    return value if isinstance(value, dict) else {"status": "idle", "trials": []}


def save_state(run_dir: str | Path, state: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**state, "updated_at": _now()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)
    return payload


def task_content_hash(task_dir: str | Path) -> str:
    """Return a stable hash of one task tree without following symlinks."""
    root = Path(task_dir).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail or (body.get("message") if isinstance(body, dict) else body))


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    **kwargs: Any,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(min(2**attempt, 4))
            continue
        if response.status_code not in _TRANSIENT or attempt + 1 >= attempts:
            return response
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else min(2**attempt, 4)
        except ValueError:
            delay = min(2**attempt, 4)
        time.sleep(min(max(delay, 0), 10))
    raise OddishError(f"Could not reach Oddish: {last_error}")


def _require_ok(response: httpx.Response, action: str) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        detail = _error_detail(response)
        if response.status_code == 401:
            detail = "Oddish rejected the API key. Replace it in Settings and retry."
        elif response.status_code == 403 and "publish" in detail.lower():
            detail = (
                "This Oddish key cannot publish experiments. Create a full-scope key in "
                "Oddish Settings, then retry."
            )
        raise OddishError(f"{action} failed: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OddishError(f"{action} returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise OddishError(f"{action} returned an invalid response")
    return payload


def _archive_task(task_dir: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=f"programsmith-{task_dir.name}-", suffix=".tar.gz")
    os.close(fd)
    archive = Path(name)
    with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
        for item in sorted(task_dir.iterdir(), key=lambda value: value.name):
            tar.add(item, arcname=item.name, recursive=True)
    return archive


def _upload_task(
    client: httpx.Client,
    *,
    api_url: str,
    task_dir: Path,
) -> dict[str, Any]:
    content_hash = task_content_hash(task_dir)
    init = _require_ok(
        _request(
            client,
            "POST",
            f"{api_url}/tasks/upload/init",
            json={"name": task_dir.name, "content_hash": content_hash},
        ),
        "Task upload",
    )
    init["content_hash"] = content_hash
    if init.get("content_unchanged"):
        return init

    upload_url = init.get("upload_url")
    if not isinstance(upload_url, str) or not upload_url:
        raise OddishError("Oddish did not provide a task upload URL")

    archive = _archive_task(task_dir)
    try:
        headers = dict(init.get("upload_headers") or {})
        headers.setdefault("Content-Length", str(archive.stat().st_size))
        with archive.open("rb") as body, httpx.Client(timeout=600, follow_redirects=True) as uploader:
            uploaded = _request(
                uploader,
                "PUT",
                upload_url,
                headers=headers,
                content=body,
                attempts=1,
            )
        if uploaded.status_code not in {200, 201, 204}:
            raise OddishError(f"Task storage upload failed: {_error_detail(uploaded)}")
    finally:
        archive.unlink(missing_ok=True)

    complete = {
        "task_id": init["task_id"],
        "name": init["name"],
        "version": init["version"],
        "content_hash": content_hash,
    }
    return _require_ok(
        _request(
            client,
            "POST",
            f"{api_url}/tasks/upload/complete",
            json=complete,
        ),
        "Task upload",
    )


def submit_task(
    run_dir: str | Path,
    task_dir: str | Path,
    *,
    api_key: str,
    api_url: str,
    dashboard_url: str,
    agent: str,
    model: str,
) -> dict[str, Any]:
    """Upload *task_dir*, launch one public Oddish trial, and persist the handoff."""
    rd = Path(run_dir)
    task = Path(task_dir).resolve()
    if not task.is_dir():
        raise OddishError("The exported task is missing")
    api = api_url.rstrip("/")
    dashboard = dashboard_url.rstrip("/")
    state = save_state(
        rd,
        {
            "status": "submitting",
            "agent": agent,
            "model": model,
            "task_name": task.name,
            "trials": [],
        },
    )
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        with httpx.Client(timeout=600, headers=headers, follow_redirects=True) as client:
            uploaded = _upload_task(client, api_url=api, task_dir=task)
            task_id = str(uploaded["task_id"])
            payload = {
                "task_id": task_id,
                "append_to_task": bool(uploaded.get("existing_task")),
                "configs": [{"agent": agent, "model": model, "n_trials": 1}],
                "priority": "low",
                "run_analysis": False,
                "run_probe": False,
                "gate_baselines": True,
                "publish_experiment": True,
                "content_hash": uploaded.get("content_hash") or task_content_hash(task),
            }
            idempotency_key = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            submitted = _require_ok(
                _request(
                    client,
                    "POST",
                    f"{api}/tasks/sweep",
                    json=payload,
                    headers={"Idempotency-Key": idempotency_key},
                    attempts=1,
                ),
                "Oddish run",
            )
            experiment_id = submitted.get("experiment_id")
            share: dict[str, Any] = {}
            if experiment_id:
                share_response = _request(
                    client,
                    "GET",
                    f"{api}/experiments/{quote(str(experiment_id), safe='')}/share",
                )
                share = _require_ok(share_response, "Public experiment link")

        public_token = share.get("public_token")
        public_url = f"{dashboard}/share/{public_token}" if public_token else None
        experiment_url = (
            f"{dashboard}/experiments/{quote(str(experiment_id), safe='')}"
            if experiment_id
            else f"{dashboard}/dashboard"
        )
        return save_state(
            rd,
            {
                **state,
                "status": "queued",
                "task_id": task_id,
                "experiment_id": experiment_id,
                "experiment_name": submitted.get("experiment_name"),
                "experiment_url": experiment_url,
                "public_token": public_token,
                "public_url": public_url,
                "trial_ids": submitted.get("new_trial_ids") or [],
                "trials": [],
                "error": None,
            },
        )
    except Exception as exc:
        error = str(exc) if isinstance(exc, OddishError) else f"Oddish handoff failed: {exc}"
        save_state(rd, {**state, "status": "failed", "error": error})
        raise OddishError(error) from exc


def _public_get(api_url: str, path: str) -> Any:
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = _request(client, "GET", f"{api_url.rstrip('/')}{path}")
    if response.status_code < 200 or response.status_code >= 300:
        raise OddishError(f"Could not read the public Oddish run: {_error_detail(response)}")
    try:
        return response.json()
    except ValueError as exc:
        raise OddishError("Oddish returned an invalid public-run response") from exc


def _compact_trial(trial: dict[str, Any]) -> dict[str, Any]:
    result = trial.get("result") if isinstance(trial.get("result"), dict) else {}
    return {
        "id": trial.get("id"),
        "index": trial.get("index"),
        "status": str(trial.get("status") or "queued").lower(),
        "agent": trial.get("agent"),
        "model": trial.get("model"),
        "reward": trial.get("reward"),
        "started_at": trial.get("started_at"),
        "finished_at": trial.get("finished_at"),
        "duration_seconds": trial.get("trajectory_duration_seconds"),
        "tool_calls": trial.get("total_tool_calls"),
        "cost_usd": trial.get("cost_usd") or result.get("cost_usd"),
        "error": (
            trial.get("error_message")
            or trial.get("error")
            or result.get("error")
            or result.get("harbor_exception")
        ),
    }


def refresh_state(run_dir: str | Path, *, api_url: str) -> dict[str, Any]:
    """Refresh a saved handoff through the unauthenticated public experiment API."""
    rd = Path(run_dir)
    state = load_state(rd)
    token = state.get("public_token")
    task_id = state.get("task_id")
    if not token or not task_id or state.get("status") in {"idle", "submitting", "failed"}:
        return state
    try:
        encoded_token = quote(str(token), safe="")
        encoded_task = quote(str(task_id), safe="")
        raw_trials = _public_get(
            api_url,
            f"/public/experiments/{encoded_token}/tasks/{encoded_task}/trials",
        )
        if isinstance(raw_trials, dict):
            raw_trials = raw_trials.get("trials") or []
        trials = [_compact_trial(item) for item in raw_trials if isinstance(item, dict)]
        statuses = {item["status"] for item in trials}
        if not trials or statuses <= {"queued", "pending", "retrying"}:
            status = "queued"
        elif any(value in {"running", "building", "verifying"} for value in statuses):
            status = "running"
        elif statuses and statuses <= _TERMINAL:
            status = "failed" if statuses <= {"failed", "cancelled", "canceled", "error"} else "complete"
        else:
            status = "running"
        return save_state(rd, {**state, "status": status, "trials": trials, "error": None})
    except OddishError as exc:
        # A temporary public-read failure must not erase a successfully submitted run. Keep the
        # last good payload and surface a non-terminal refresh error for the UI.
        return {**state, "refresh_error": str(exc)}


def get_trajectory(
    run_dir: str | Path,
    *,
    api_url: str,
    trial_id: str | None = None,
) -> dict[str, Any] | list[Any] | None:
    state = load_state(run_dir)
    token = state.get("public_token")
    selected = trial_id or next(
        (str(item.get("id")) for item in state.get("trials", []) if item.get("id")),
        None,
    )
    if not token or not selected:
        return None
    return _public_get(
        api_url,
        f"/public/experiments/{quote(str(token), safe='')}/trials/"
        f"{quote(selected, safe='')}/trajectory",
    )
