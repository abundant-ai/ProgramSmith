"""Validation for run directory keys supplied through the CLI or HTTP API."""

from __future__ import annotations

import re

_RUN_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_run_key(value: str) -> str:
    """Return a normalized safe run key or raise ``ValueError``.

    Run keys become directory names, so separators, traversal components, control characters, and
    empty values are rejected at the boundary.
    """
    key = value.strip()
    if not _RUN_KEY.fullmatch(key):
        raise ValueError(
            "run name must start with a letter or number and contain only letters, numbers, '.', "
            "'_' or '-' (maximum 128 characters)"
        )
    return key
