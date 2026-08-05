"""Guardrails: non-ProgramBench tasks must not target official ProgramBench upstreams.

Vendored from programbench-farm/programbench_guard.py (invariant #3). Adapted for library use:
`assert_not_programbench` (SystemExit) becomes `check_not_programbench` returning (ok, reason) —
a gate (INGEST) turns the tuple into a verdict; library code must never kill the orchestrator
process. The near-miss rule is HANDOFF.md §4's lookalike warning (`astaxie/bat` vs `sharkdp/bat`,
`luajit/luajit` vs `lua/lua`): a repo NAME that matches an official entry under a DIFFERENT owner
is allowed but flagged, so a human skimming the run log catches an accidental lookalike pick.

The canonical org/repo list lives in data/programbench_repos.txt (sourced from
https://huggingface.co/datasets/programbench/ProgramBench-Tests). The farm's grandfathered-slugs
list is intentionally NOT vendored: it is empty upstream ("all previously grandfathered tasks
were dropped"), so there is no exemption path here.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPOS_FILE = Path(__file__).parent / "data" / "programbench_repos.txt"
_GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([^/\s#?]+)/([^/\s#?.]+)",
    re.IGNORECASE,
)


def _load_programbench_repos() -> frozenset[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for line in _REPOS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "/" not in line:
            raise ValueError(f"invalid programbench_repos.txt line: {line!r}")
        owner, repo = line.split("/", 1)
        pairs.add((owner.lower(), repo.lower()))
    if not pairs:
        raise ValueError(f"empty ProgramBench repo list: {_REPOS_FILE}")
    return frozenset(pairs)


PROGRAMBENCH_REPOS: frozenset[tuple[str, str]] = _load_programbench_repos()


def parse_github_repo(url_or_repo: str) -> tuple[str, str]:
    """Return (owner, repo) from a GitHub URL or 'owner/repo' string."""
    s = url_or_repo.strip().rstrip("/")
    if s.startswith("git@"):
        m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", s, re.I)
        if m:
            return m.group(1).lower(), m.group(2).lower()
    if "/" in s and "github.com" not in s and "://" not in s:
        owner, repo = s.split("/", 1)
        return owner.lower(), repo.removesuffix(".git").lower()
    m = _GITHUB_RE.search(s)
    if not m:
        raise ValueError(f"cannot parse GitHub org/repo from {url_or_repo!r}")
    return m.group(1).lower(), m.group(2).lower()


def programbench_entry_id(owner: str, repo: str) -> str:
    """Directory name prefix used in ProgramBench-Tests (org__repo.<commit>)."""
    return f"{owner}__{repo}"


def is_programbench_upstream(url_or_repo: str) -> bool:
    owner, repo = parse_github_repo(url_or_repo)
    return (owner, repo) in PROGRAMBENCH_REPOS


def check_not_programbench(
    url_or_repo: str,
    *,
    context: str = "",
    slug: str | None = None,
) -> tuple[bool, str]:
    """(ok, reason) — ok=False iff url_or_repo is an official ProgramBench upstream.

    ok=True with a "near-miss" reason when only the repo NAME collides (different owner) —
    allowed, but the caller should surface the warning. Non-GitHub refs can't be in the
    (GitHub-only) official list, so an unparseable ref is ok with a skipped-guard note —
    the guard fails open on parse, closed on membership.
    """
    where = f" ({context})" if context else ""
    try:
        owner, repo = parse_github_repo(url_or_repo)
    except ValueError as e:
        return True, f"guard skipped{where}: {e}"
    if (owner, repo) in PROGRAMBENCH_REPOS:
        entry = programbench_entry_id(owner, repo)
        return False, (
            f"upstream {owner}/{repo} is in the official ProgramBench dataset"
            f"{where} (ProgramBench-Tests entry prefix {entry!r}). Pick a different "
            f"upstream — see programbench/data/programbench_repos.txt and HANDOFF.md §4."
        )
    lookalikes = sorted(f"{o}/{r}" for (o, r) in PROGRAMBENCH_REPOS if r == repo)
    if lookalikes:
        return True, (
            f"near-miss{where}: {owner}/{repo} shares its repo name with official "
            f"ProgramBench entr{'ies' if len(lookalikes) > 1 else 'y'} "
            f"{', '.join(lookalikes)} — verify this is a genuinely different project "
            f"(astaxie/bat-vs-sharkdp/bat class, HANDOFF.md §4)."
        )
    return True, f"not a ProgramBench upstream: {owner}/{repo}"
