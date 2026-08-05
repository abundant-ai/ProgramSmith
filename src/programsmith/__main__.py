"""Module entrypoint so `python -m programsmith …` mirrors the `programsmith` console script.

Used by the auto-started dashboard (cli._ensure_dashboard) to spawn `serve` with the exact
interpreter running the foreground command — robust across editable/venv/tool installs where the
`programsmith` script may not be on PATH."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
