"""Reap orphaned agentic cell agents leaked by a prior server.

`programsmith serve` spawns headless `claude -p` cell agents (CREATE-fill, SYNTHESIZE harden) as child processes
in daemon threads. When the server is stopped/restarted/crashes, those children DON'T die — they
reparent to init (PPID 1) and keep running, editing a run's `task/` dir. The next server then respawns
the same job (the watchdog demotes the orphaned job + relaunches), so the run ends up DOUBLE-AGENTED:
the orphan and the fresh agent edit the same files concurrently → a race that stalls the synthesize.

This module kills those orphans at server startup. The match is deliberately narrow so it can NEVER
touch the host Claude Code session (that's `claude --output-format stream-json`, not `claude -p`, and
carries no FOCUS_PROMPT) nor a live side-server's cells (their PPID is that server, not 1):
  - the command is a headless cell (`claude -p` or the current `claude --bare -p`),
  - it carries our FOCUS_PROMPT signature (so it's OURS, not some other `claude -p`),
  - it is orphaned (PPID == 1 → its spawning server is gone).
"""

from __future__ import annotations

import os
import signal
import subprocess

# A stable, unique substring of cells.agentic.FOCUS_PROMPT — identifies a process as one of OUR cells
# without importing (and without matching on the whole prompt, which `ps` may truncate differently).
_FOCUS_SIGNATURE = "headless worker executing ONE narrowly-scoped task"


def orphan_cell_pids(ps_lines: list[str]) -> list[int]:
    """Pure parser over `ps -axo pid,ppid,command` lines → PIDs of orphaned cell agents to reap.
    Orphaned == PPID 1 (the spawning server died and the child reparented to init). Kept pure so the
    matching is unit-tested without touching real processes."""
    pids: list[int] = []
    for line in ps_lines:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if not pid_s.isdigit() or ppid_s != "1":     # only orphans (parent gone) — never a live cell
            continue
        # Claude Code now inserts `--bare` before `-p`, so substring-matching `claude -p` leaked
        # every current worker across a restart. Match the actual print-mode argument as a token;
        # the PPID and unique focus signature keep this just as narrow.
        if "-p" not in cmd.split() or _FOCUS_SIGNATURE not in cmd:  # only OUR headless cells
            continue
        pids.append(int(pid_s))
    return pids


def reap_orphan_cell_agents() -> list[int]:
    """Find + SIGKILL orphaned cell agents from a prior server. Returns the reaped PIDs (for logging).
    Best-effort: a `ps` failure or an already-gone PID is swallowed — reaping must never block startup."""
    try:
        out = subprocess.run(["ps", "-axo", "pid,ppid,command"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    reaped: list[int] = []
    for pid in orphan_cell_pids(out.splitlines()):
        try:
            os.kill(pid, signal.SIGKILL)   # claude -p ignores SIGTERM; SIGKILL is the reliable reap
            reaped.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return reaped
