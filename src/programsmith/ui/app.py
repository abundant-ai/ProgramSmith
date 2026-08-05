"""FastAPI app — JSON API (/api/*) + serves the React SPA build (ADR-0018).

Run:  programsmith serve   (or: uvicorn programsmith.ui.app:app)
The React app is built to `ui/frontend/dist`; if absent, the root returns an API hint so the
backend is usable on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import prime_fleet_cache, router


async def _autodrive_loop() -> None:
    """Background auto-driver (gated on PROGRAMSMITH_AUTODRIVE=1, which `programsmith serve` sets by default): advance the
    fleet one drive() pass per interval, hands-free, so runs flow forward on REAL results and halt
    only at the two human gates / a pause / a stage that can't run here. There is NO synthetic path —
    evaluation sweeps LAUNCH for real on the operator's own key only when
    PROGRAMSMITH_AUTODRIVE_SPEND=1. Agentic task-generation cells use the Anthropic credential
    whenever agentic autodrive is enabled, independently of the sweep switch.
    `autodrive_once` runs in a thread so the blocking drive never stalls the event loop. The env gate
    keeps the loop OFF when the app is merely imported (e.g. in tests) — only `programsmith serve` turns it on."""
    from ..config import LhConfig
    from ..daemon import autodrive_once
    interval = float(os.getenv("PROGRAMSMITH_AUTODRIVE_INTERVAL", "8"))
    # Structural switches are fixed at serve launch (env). The TUNING knobs (ci_repo_root fallback,
    # sweep depth, harden-review policy) are re-read from config EACH PASS, so a Settings change takes
    # effect live without a server restart.
    spend = os.getenv("PROGRAMSMITH_AUTODRIVE_SPEND", "1") != "0"
    agentic = os.getenv("PROGRAMSMITH_AGENTIC", "1") != "0"
    while True:
        try:
            cfg = LhConfig.load()
            ctx: dict = {}
            if spend:
                # Authorize real sweeps to launch (the operator's decision — trials bill their key).
                ctx["sweep_live"] = True
            if agentic:
                # Clean-room generate-mode (ORACLE) + agentic CREATE-fill, as NON-BLOCKING bg jobs.
                ctx["agentic"] = True
                ctx["agentic_background"] = True
            if cfg.ci_repo_root:            # env PROGRAMSMITH_CI_REPO_ROOT is already folded into cfg.ci_repo_root
                ctx["ci_repo_root"] = cfg.ci_repo_root
            ctx["difficulty_trials"] = cfg.difficulty_trials
            ctx["full_trials"] = cfg.full_trials
            ctx["harden_drop_after"] = cfg.harden_drop_after
            ctx["harden_min_improvement"] = cfg.harden_min_improvement
            runs_dir = cfg.runs_dir
            await asyncio.to_thread(
                autodrive_once, runs_dir, ctx=ctx,
                notes_path=Path(runs_dir).parent / "WORKFLOW_NOTES.md",
            )
        except Exception:  # noqa: BLE001 — a bad pass must not kill the loop
            pass
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Reap orphaned cell agents leaked by a PRIOR server (stop/restart/crash): headless `claude -p`
    # children reparent to init and keep editing run task/ dirs, racing the fresh agents this server
    # spawns (a double-agented run stalls). Kill them BEFORE the auto-driver relaunches anything.
    from ..procreap import reap_orphan_cell_agents
    if reaped := reap_orphan_cell_agents():
        print(f"[UI] reaped {len(reaped)} orphaned cell agent(s) from a prior server: {reaped}")
    # Build the expensive first fleet snapshot before the server advertises itself as ready. The
    # UI then receives an immediate cached response instead of rendering a blank page while a large
    # farm is scanned. Later snapshots use stale-while-revalidate and never block readers.
    try:
        await asyncio.to_thread(prime_fleet_cache)
    except Exception:  # noqa: BLE001 — a damaged run must not prevent the dashboard from starting
        pass
    task = None
    if os.getenv("PROGRAMSMITH_AUTODRIVE") == "1":
        task = asyncio.create_task(_autodrive_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# docs_url/redoc_url disabled so the SPA owns /docs (the in-app Documentation tab) instead of
# FastAPI's auto Swagger UI shadowing it on a full page load. The OpenAPI schema stays at
# /openapi.json for tooling.
app = FastAPI(title="ProgramSmith", lifespan=_lifespan, docs_url=None, redoc_url=None)
app.include_router(router)

# The built SPA. Default: next to the live source (`programsmith serve` / local dev / the installed wheel).
# PROGRAMSMITH_FRONTEND_DIST overrides for a dist built elsewhere.
FRONTEND_DIST = Path(os.getenv("PROGRAMSMITH_FRONTEND_DIST") or (Path(__file__).parent / "frontend" / "dist"))


@app.get("/api/health")
def health() -> dict:
    from ..config import LhConfig
    return {
        "ok": True,
        "frontend_built": FRONTEND_DIST.exists(),
        "runs_dir": str(Path(LhConfig.load().runs_dir).expanduser().resolve()),
        "pid": os.getpid(),
        "instance_id": os.getenv("PROGRAMSMITH_DASHBOARD_TOKEN"),
        "autodrive": os.getenv("PROGRAMSMITH_AUTODRIVE") == "1",
        "autodrive_interval": float(os.getenv("PROGRAMSMITH_AUTODRIVE_INTERVAL", "8")),
        "spend": os.getenv("PROGRAMSMITH_AUTODRIVE_SPEND", "0") == "1",
    }


if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


@app.get("/{full_path:path}")
def spa(full_path: str):
    """Serve built static files, else fall back to index.html (client-side routing). Never shadows
    /api/* (those are matched by the router, registered first)."""
    if full_path.startswith("api/"):
        raise HTTPException(404, "unknown API route")
    if not FRONTEND_DIST.exists():
        return JSONResponse({
            "message": "ProgramSmith API. The React UI is not built yet "
                       "(build to ui/frontend/dist). Use the JSON API directly:",
            "api": ["/api/preflight", "/api/settings", "/api/runs", "/api/runs/{key}"],
        })
    candidate = FRONTEND_DIST / full_path
    if full_path and candidate.is_file():
        return FileResponse(str(candidate))
    return FileResponse(str(FRONTEND_DIST / "index.html"))
