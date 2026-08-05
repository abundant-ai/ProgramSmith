"""Persist the exact prompt each LLM cell used, so the UI step inspector can show 'the specific
prompt' for a step (opened in the side file viewer). Written under `<run>/prompts/<stage>.md`, which
the existing files API/FileExplorer already serves. Best-effort — a persistence failure must never
break a run, so every write is guarded."""

from __future__ import annotations

from pathlib import Path


def prompt_path(run_dir: Path | str, stage: str) -> Path:
    """The canonical on-disk location of a stage's persisted prompt (relative-safe, no path
    traversal — `stage` is a fixed pipeline identifier, never user input)."""
    return Path(run_dir) / "prompts" / f"{stage}.md"


def write_prompt(run_dir: Path | str, stage: str, text: str) -> None:
    """Persist `text` as the prompt for `stage`. Never raises."""
    try:
        p = prompt_path(run_dir, stage)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if text.endswith("\n") else text + "\n")
    except Exception:  # noqa: BLE001 — persistence is advisory; a run must not fail on it
        pass
