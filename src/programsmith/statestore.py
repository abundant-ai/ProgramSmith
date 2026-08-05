"""Pluggable backend for the orchestrator's state.

The fleet's state lives in local files with atomic `tmp + os.replace` writes:
  - control-plane JSON: state.json / manifest.json / jobs.json / drive.json (+ config)
  - the working/task tree: the generated task dir, source clone, agent-logs

This module defines a small `StateStore` over the operations the code actually performs — read /
write-atomic / exists / list / delete — with a `LocalFileStore` default. Two facts keep this simple
(no DB, no locking):
  * the orchestrator is a SINGLE fleet-driver process → single-writer-per-run → atomic per-object
    writes suffice;
  * any mounted filesystem needs NO new impl — `LocalFileStore(mount_path)` IS that backend.

The Protocol is the SEAM: call sites (state.py / manifest.py / jobs.py / ui.store) route their
control-plane files through `store_for`, so an alternative store is a drop-in without touching
them. The live WORKING TREE (task dir, agent-logs, source clone) stays a real filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class StateStore(Protocol):
    """The minimal surface the orchestrator needs over its state. Paths are POSIX-relative to the
    store root (e.g. ``"<run_key>/jobs.json"``)."""

    def read(self, path: str) -> str | None: ...
    def read_bytes(self, path: str) -> bytes | None: ...
    def write_atomic(self, path: str, content: str | bytes) -> None: ...
    def exists(self, path: str) -> bool: ...
    def list_dir(self, path: str) -> list[str]: ...
    def delete(self, path: str) -> None: ...
    def delete_tree(self, path: str) -> None: ...


class LocalFileStore:
    """Default backend: the local filesystem rooted at ``root``, with atomic tmp + os.replace
    publishes."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _p(self, path: str) -> Path:
        return self.root / path

    def read(self, path: str) -> str | None:
        p = self._p(path)
        return p.read_text() if p.is_file() else None

    def read_bytes(self, path: str) -> bytes | None:
        p = self._p(path)
        return p.read_bytes() if p.is_file() else None

    def write_atomic(self, path: str, content: str | bytes) -> None:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        if isinstance(content, bytes):
            tmp.write_bytes(content)
        else:
            tmp.write_text(content)
        os.replace(tmp, p)          # atomic publish (POSIX rename)

    def exists(self, path: str) -> bool:
        return self._p(path).exists()

    def list_dir(self, path: str) -> list[str]:
        d = self._p(path)
        return sorted(c.name for c in d.iterdir()) if d.is_dir() else []

    def delete(self, path: str) -> None:
        p = self._p(path)
        if p.exists():
            p.unlink()

    def delete_tree(self, path: str) -> None:
        import shutil
        p = self._p(path)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def get_store(root: str | Path) -> StateStore:
    """Resolve the state store: a LocalFileStore rooted at ``root``."""
    return LocalFileStore(root)


def store_for(run_dir: str | Path) -> tuple[StateStore, str]:
    """Resolve (store, run_key) for a run's control-plane files (state/manifest/jobs/drive json).

    A control-plane file at ``<run_dir>/<name>`` is addressed as ``store.<op>(f"{run_key}/{name}")``
    — `LocalFileStore(run_dir.parent)` + key `run_dir.name/<name>` resolves to exactly
    `run_dir/<name>`, so on-disk state stays byte-for-byte where the path-based code put it."""
    run_dir = Path(run_dir)
    return LocalFileStore(run_dir.parent), run_dir.name


def run_state_exists(run_dir: str | Path) -> bool:
    """True if a run's state.json exists in the store — the 'does this run exist?' guard.
    Store-agnostic replacement for `(run_dir / "state.json").exists()`."""
    store, key = store_for(run_dir)
    return store.exists(f"{key}/state.json")


def delete_run(run_dir: str | Path) -> None:
    """Remove a run ENTIRELY: its control-plane subtree in the store (state/manifest/jobs/drive) AND
    its live working tree on the real filesystem (task dir, source clone, agent logs, sweep
    artifacts). On the local backend these coincide (one rmtree). Irreversible — the caller confirms
    intent."""
    import shutil
    run_dir = Path(run_dir)
    store, key = store_for(run_dir)
    store.delete_tree(key)                          # control-plane
    shutil.rmtree(run_dir, ignore_errors=True)      # the live working tree
