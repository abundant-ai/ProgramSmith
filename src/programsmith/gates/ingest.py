"""INGEST + LOCK gate (deterministic).

Clone the source repo at a pinned SHA, deterministically detect language / build / test
framework, classify the license, record size — and emit `pass`/`fail`. Populates the manifest's
`SourceInfo`. ADR-0038 adds two ProgramBench checks: the official-upstream GUARD (a repo already
in ProgramBench-Tests is a hard fail unless LhConfig.allow_programbench_overlap) and an ADVISORY
`has_cli_entrypoint` heuristic recorded for TASK MATRIX (the new genre needs a real CLI tool,
not a library — advisory only, the cell decides).

Reuse basis (invariant #3): the clone-at-SHA flow follows SWE-gen's
`src/swegen/create/repo_cache.py::RepoCache.get_or_clone(repo, head_sha, repo_url)`; the
language-agnostic detection follows `src/swegen/create/utils.py` (Python/JS/TS/Go/Rust/Ruby/Java/
C/C++/PHP/C#, here extended with Fortran/Zig). License-class gate per ADR-0010 (drops copyleft;
the upstream 02-ideas gate dropped GPL/LGPL/AGPL). Guard: vendored programbench-farm
programbench_guard (programsmith.programbench.guard).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from ..manifest import Manifest, SourceInfo
from ..programbench.guard import check_not_programbench
from ..source_screen import detect_cli_entrypoint
from . import GateResult

# --- extension → language -------------------------------------------------------------
_EXT_LANG = {
    ".f90": "Fortran", ".f95": "Fortran", ".f03": "Fortran", ".f08": "Fortran",
    ".f": "Fortran", ".for": "Fortran", ".f77": "Fortran", ".fpp": "Fortran",
    ".rs": "Rust", ".go": "Go", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".py": "Python", ".rb": "Ruby",
    ".java": "Java", ".kt": "Kotlin", ".c": "C", ".h": "C", ".cc": "C++",
    ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".zig": "Zig",
    ".cs": "C#", ".php": "PHP", ".swift": "Swift", ".scala": "Scala",
}

# --- build-system marker file → name --------------------------------------------------
_BUILD_MARKERS = {
    "Cargo.toml": "cargo", "go.mod": "go", "package.json": "node",
    "pyproject.toml": "python", "setup.py": "python", "fpm.toml": "fpm",
    "meson.build": "meson", "CMakeLists.txt": "cmake", "Makefile": "make",
    "makefile": "make", "pom.xml": "maven", "build.gradle": "gradle",
    "build.zig": "zig", "Gemfile": "bundler", "configure.ac": "autotools",
}

# --- license fingerprint → (display name, class) --------------------------------------
# Split into PERMISSIVE and COPYLEFT rule groups. classify sees BOTH groups and, within a single
# blob, a permissive signal WINS over a copyleft one — because a file carrying both (a dual-licensed
# LICENSE like mbedTLS's "Apache-2.0 OR GPL-2.0", or a permissive license whose text merely *mentions*
# the GPL like Python's PSF license) offers a usable permissive path. This is the single most common
# false-drop: an embedded GPL preamble ("...Lesser General Public License...") matching before the
# Apache/PSF grant in the same file. Order WITHIN each group is specificity (most specific first).
_PERMISSIVE_RULES: list[tuple[str, str]] = [
    ("APACHE LICENSE", "Apache-2.0"),
    ("LICENSED UNDER THE APACHE LICENSE", "Apache-2.0"),
    ("MIT LICENSE", "MIT"),
    ("PERMISSION IS HEREBY GRANTED, FREE OF CHARGE", "MIT"),
    # "Old MIT" / X11-style: "Permission is hereby granted, WITHOUT written agreement..." (harfbuzz, X11)
    ("PERMISSION IS HEREBY GRANTED, WITHOUT", "Old-MIT"),
    ("WITHOUT LICENSE OR ROYALTY FEES", "Old-MIT"),
    # zlib license — its distinctive grant + marking clause (no literal "zlib license" string)
    ("PERMISSION IS GRANTED TO ANYONE TO USE THIS SOFTWARE", "Zlib"),
    ("ALTERED SOURCE VERSIONS MUST BE PLAINLY MARKED", "Zlib"),
    ("ZLIB LICENSE", "Zlib"),
    ("ZLIB/LIBPNG", "Zlib/libpng"),
    ("PNG REFERENCE LIBRARY", "libpng"),
    ("LICENSE FOR LIBPNG", "libpng"),
    # Python PSF license (permissive; its text MENTIONS "GPL-compatible", hence must win over GPL)
    ("PYTHON SOFTWARE FOUNDATION LICENSE", "PSF-2.0"),
    ("PSF LICENSE AGREEMENT", "PSF-2.0"),
    ("BEOPEN.COM LICENSE AGREEMENT", "PSF-2.0"),
    ("REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS", "BSD"),
    ("ISC LICENSE", "ISC"),
    ("PERMISSION TO USE, COPY, MODIFY", "permissive-grant"),   # X11/ISC-style grant (One True AWK)
    ("OPENLDAP PUBLIC LICENSE", "OLDAP"),                       # LMDB / openldap
    ("BOOST SOFTWARE LICENSE", "BSL-1.0"),
    ("THE UNLICENSE", "Unlicense"),
    ("CREATIVE COMMONS LEGAL CODE\n\nCC0", "CC0"),
    ("PUBLIC DOMAIN", "public-domain"),
    ("MINPACK", "Minpack-1.0 (permissive)"),
    ("BSD", "BSD"),                                             # last: bare token, least specific
]
_COPYLEFT_RULES: list[tuple[str, str, str]] = [
    ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL", "strong-copyleft"),
    ("AFFERO GENERAL PUBLIC LICENSE", "AGPL", "strong-copyleft"),
    ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL", "weak-copyleft"),
    ("LESSER GENERAL PUBLIC LICENSE", "LGPL", "weak-copyleft"),
    ("LIBRARY GENERAL PUBLIC LICENSE", "LGPL", "weak-copyleft"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL", "strong-copyleft"),
    ("MOZILLA PUBLIC LICENSE", "MPL", "weak-copyleft"),
    ("ECLIPSE PUBLIC LICENSE", "EPL", "weak-copyleft"),
]

# SPDX-License-Identifier tags (modern, unambiguous). A dual expression ("Apache-2.0 OR GPL-2.0")
# with any permissive id present → permissive. Highest-confidence signal, checked before fingerprints.
_SPDX_PERMISSIVE = ("APACHE-2.0", "MIT", "MIT-0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "0BSD", "ISC",
                    "ZLIB", "LIBPNG", "UNLICENSE", "PSF-2.0", "PYTHON-2.0", "CC0-1.0", "BSL-1.0",
                    "OLDAP-2.1", "CURL", "X11", "BSD")
_SPDX_COPYLEFT: list[tuple[str, str]] = [
    ("AGPL", "strong-copyleft"), ("LGPL", "weak-copyleft"), ("GPL", "strong-copyleft"),
    ("MPL", "weak-copyleft"), ("EPL", "weak-copyleft"),
]

_SKIP_DIRS = {".git", "node_modules", "target", "build", ".venv", "venv", "dist", "__pycache__"}


# A git op must never hang the (synchronous) ingest/farm forever — a slow or wedged network op
# on ONE big repo would stall every remaining repo behind it. Bound every git call; a clone gets a
# longer budget than a metadata op. Callers turn TimeoutExpired into a clean gate "fail" (drop this
# repo, continue the farm), same as any other clone failure.
_GIT_TIMEOUT = int(os.getenv("PROGRAMSMITH_GIT_TIMEOUT", "120"))          # metadata ops (rev-parse/checkout/fetch)
CLONE_TIMEOUT = int(os.getenv("PROGRAMSMITH_CLONE_TIMEOUT", "300"))       # the clone itself (5 min; treeless is fast)


def _git(args: list[str], cwd: Path | None = None, *, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          timeout=timeout)


def clone_at_sha(repo_url: str, head_sha: str, dest: Path) -> Path:
    """Clone `repo_url` into `dest` and check out `head_sha`. Idempotent: a present, correctly
    pinned checkout is reused (SWE-gen RepoCache pattern).

    Uses a TREELESS partial clone (`--filter=blob:none`): every commit + tree object is fetched
    (so ANY pinned SHA is checkoutable), but file blobs are lazy-fetched only for the revision we
    check out — never the full history of every file. A full clone of a large repo (wasm-tools is
    hundreds of MB of history) becomes a few MB, which is what lets a synchronous farm clone 50+
    repos without one big repo stalling the rest. `--no-tags` trims further. Bounded by CLONE_TIMEOUT
    so a wedged network op fails this ONE repo (clean gate 'fail') instead of hanging the farm."""
    dest = Path(dest)
    if (dest / ".git").exists():
        try:
            cur = _git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
            if cur.startswith(head_sha) or head_sha.startswith(cur):
                return dest
        except subprocess.CalledProcessError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        _git(["clone", "--filter=blob:none", "--no-tags", repo_url, str(dest)], timeout=CLONE_TIMEOUT)
    try:
        _git(["checkout", "--quiet", head_sha], cwd=dest, timeout=CLONE_TIMEOUT)
    except subprocess.CalledProcessError:
        _git(["fetch", "--quiet", "origin", head_sha], cwd=dest, timeout=CLONE_TIMEOUT)
        _git(["checkout", "--quiet", head_sha], cwd=dest, timeout=CLONE_TIMEOUT)
    return dest


def _iter_source_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
            yield p


def detect_language(root: Path) -> tuple[str | None, Counter]:
    counts: Counter = Counter()
    for p in _iter_source_files(root):
        lang = _EXT_LANG.get(p.suffix.lower())
        if lang:
            counts[lang] += 1
    primary = counts.most_common(1)[0][0] if counts else None
    return primary, counts


def detect_build_systems(root: Path) -> list[str]:
    found: list[str] = []
    for marker, name in _BUILD_MARKERS.items():
        if (root / marker).exists() or any(root.rglob(marker)):
            if name not in found:
                found.append(name)
    return found


def detect_test_frameworks(root: Path, build_systems: list[str]) -> list[str]:
    fws: list[str] = []
    has_test_dir = any((root / d).is_dir() for d in ("test", "tests", "spec", "__tests__"))
    # build-system-implied test runners
    if "cargo" in build_systems:
        fws.append("cargo-test")
    if "go" in build_systems:
        fws.append("go-test")
    if "python" in build_systems and (any(root.rglob("test_*.py")) or any(root.rglob("*_test.py"))):
        fws.append("pytest")
    if "node" in build_systems:
        fws.append("npm-test")
    if has_test_dir and not fws:
        fws.append("test-dir")
    return fws


# Top-level license-file name MATCHERS (matched case-INSENSITIVELY against the lowercased filename —
# so `Copyright` (libxml2), `License`, `Licence` (British), `COPYING.Xiph` (flac) all match on a
# case-sensitive filesystem). Substrings, not globs.
_LICENSE_NAME_HINTS = ("license", "licence", "copying", "copyright", "notice", "unlicense")
# Fallback evidence when NO license file is recognized: some projects (Lua, zlib, libpng) ship the
# grant ONLY in a canonical source header (e.g. `lua.h`), and every other file just says "See
# Copyright Notice in lua.h". So the fallback scans README + top-level source files, PRIORITIZING
# headers, and FOLLOWS a "see copyright notice in <file>" reference to the file that actually holds
# the grant. It uses a STRICT permissive matcher (full grants / SPDX / license titles only — never the
# bare 'BSD' token) so a stray mention in code can't mislabel; copyleft-only projects ship a COPYING
# and never reach here, so "no grant found" conservatively stays a drop.
_FALLBACK_SUFFIXES = (".h", ".hpp", ".c", ".cc", ".cpp", ".rs", ".go", ".py")
# Permissive needles too loose to trust in a source-file scan (a bare token could appear in a code
# comment). Excluded from the fallback's STRICT matcher; still used for actual LICENSE files.
_LOOSE_NEEDLES = frozenset({"BSD", "PUBLIC DOMAIN", "MINPACK"})
_SPDX_RE = re.compile(r"SPDX-LICENSE-IDENTIFIER:\s*([A-Z0-9.\-+ ()]+)")
_NOTICE_REF_RE = re.compile(r"COPYRIGHT NOTICE (?:IN|AT THE END OF|AT) ([A-Z0-9_.\-]+\.[A-Z]+)")


def _spdx_class(text_upper: str) -> tuple[str, str] | None:
    """Classify from an SPDX-License-Identifier tag if present. Dual expr with any permissive id →
    permissive (name = that id); else the first copyleft family id → its class."""
    m = _SPDX_RE.search(text_upper)
    if not m:
        return None
    expr = m.group(1)
    for pid in _SPDX_PERMISSIVE:
        if pid in expr:
            return pid.title(), "permissive"
    for cid, klass in _SPDX_COPYLEFT:
        if cid in expr:
            return cid, klass
    return None


def _classify_text(text_upper: str) -> tuple[str, str] | None:
    """Best (display_name, class) for one license blob. SPDX wins; else a PERMISSIVE fingerprint wins
    over a copyleft one in the SAME blob (dual-license / GPL-mention-in-a-permissive-license); else a
    copyleft fingerprint. None when nothing matches."""
    if spdx := _spdx_class(text_upper):
        return spdx
    for needle, display in _PERMISSIVE_RULES:
        if needle in text_upper:
            return display, "permissive"
    for needle, display, klass in _COPYLEFT_RULES:
        if needle in text_upper:
            return display, klass
    return None


def _strict_permissive(text_upper: str) -> tuple[str, str] | None:
    """PERMISSIVE-only classification for the fallback source-header scan: SPDX + full-grant/title
    fingerprints, skipping the loose single-token needles. Never returns copyleft (a copyleft-only
    project has a COPYING file and never reaches the fallback)."""
    if (spdx := _spdx_class(text_upper)) and spdx[1] == "permissive":
        return spdx
    for needle, display in _PERMISSIVE_RULES:
        if needle not in _LOOSE_NEEDLES and needle in text_upper:
            return display, "permissive"
    return None


def _fallback_candidates(root: Path) -> list[Path]:
    """Top-level files to scan for a license grant when no license file was recognized, ranked so the
    grant-bearing file is reached before a large tail of source files gets cut by the cap: README
    first, then license/notice-named files, then headers (where Lua/zlib/png keep the grant), then
    other sources. Capped for cost (only small/old repos with NO license file reach here)."""
    try:
        entries = [p for p in root.iterdir() if p.is_file()]
    except OSError:
        return []

    def rank(p: Path) -> int:
        low = p.name.lower()
        if "readme" in low:
            return 0
        if any(h in low for h in ("licen", "copy", "notice")):
            return 1
        if p.suffix.lower() in (".h", ".hpp"):
            return 2
        if p.suffix.lower() in _FALLBACK_SUFFIXES:
            return 3
        return 9

    cands = sorted((p for p in entries if rank(p) < 9), key=lambda p: (rank(p), p.name.lower()))
    return cands[:80]


def _top_level_files(root: Path, name_hints: tuple[str, ...], suffixes: tuple[str, ...] = ()) -> list[Path]:
    """Top-level files whose lowercased name CONTAINS a hint, or whose suffix is in `suffixes`.
    Case-insensitive; top-level only (never a vendored dep's nested LICENSE)."""
    out: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for p in entries:
        if not p.is_file():
            continue
        low = p.name.lower()
        if any(h in low for h in name_hints) or (suffixes and p.suffix.lower() in suffixes):
            out.append(p)
    return out


def classify_license(root: Path) -> tuple[str | None, str, str | None]:
    """Return (license_name, license_class, license_filename).

    Robust, evidence-based: (1) scan ALL top-level license files (case-insensitive names) — for a
    DUAL-licensed project a PERMISSIVE option wins (mbedTLS `Apache-2.0 OR GPL-2.0`, rocksdb
    `LICENSE.Apache` + GPL `COPYING`), and a permissive license that merely *mentions* the GPL (Python
    PSF) is NOT misread as copyleft; (2) if no license file is recognized, FALL BACK to the README +
    top-level source headers (Lua ships its MIT grant only in `lua.h`). A source is copyleft ONLY when
    the sole evidence is copyleft; 'unrecognized' only when files exist but none matches."""
    permissive: tuple[str, str, str] | None = None
    copyleft: tuple[str, str, str] | None = None
    seen_unmatched: str | None = None

    def _consider(path: Path) -> None:
        nonlocal permissive, copyleft, seen_unmatched
        try:
            text = path.read_text(errors="ignore").upper()
        except OSError:
            return
        hit = _classify_text(text)
        if hit is None:
            seen_unmatched = seen_unmatched or path.name
        elif hit[1] == "permissive":
            permissive = permissive or (hit[0], hit[1], path.name)
        else:
            copyleft = copyleft or (hit[0], hit[1], path.name)

    for path in _top_level_files(root, _LICENSE_NAME_HINTS):
        _consider(path)
    if permissive:          # dual-licensed (permissive ∨ copyleft) → the permissive path is usable
        return permissive
    if copyleft:
        return copyleft

    # No license file recognized → fall back to README + top-level source headers. Scan HEAD + TAIL
    # (grants are often end-of-file notices — Lua's `lua.h`), strict-permissive only, and FOLLOW a
    # "see copyright notice in <file>" reference to the file that actually holds the grant.
    referenced: set[str] = set()
    for path in _fallback_candidates(root):
        try:
            raw = path.read_text(errors="ignore")
        except OSError:
            continue
        blob = (raw[:4000] + "\n" + raw[-4000:]).upper()
        if hit := _strict_permissive(blob):
            return hit[0], hit[1], path.name
        for ref in _NOTICE_REF_RE.findall(blob):
            referenced.add(ref.lower())
    # a source header pointed at another top-level file for the grant (e.g. "... in lua.h") — read it
    for path in root.iterdir() if referenced else []:
        if path.is_file() and path.name.lower() in referenced:
            try:
                raw = path.read_text(errors="ignore")
            except OSError:
                continue
            blob = (raw[:4000] + "\n" + raw[-4000:]).upper()
            if hit := _strict_permissive(blob):
                return hit[0], hit[1], path.name

    if seen_unmatched:      # license file(s) present but none recognized → needs human classification
        return "unrecognized", "unknown", seen_unmatched
    return None, "unknown", None


def _count_loc(root: Path, primary_language: str | None) -> tuple[int, int]:
    files = 0
    loc = 0
    target_exts = {ext for ext, lang in _EXT_LANG.items() if lang == primary_language}
    for p in _iter_source_files(root):
        if p.suffix.lower() in target_exts:
            files += 1
            try:
                loc += sum(1 for _ in p.open("rb"))
            except OSError:
                pass
    return files, loc


def ingest(
    manifest: Manifest,
    repo: str,
    head_sha: str,
    work_dir: Path,
    repo_url: str | None = None,
    local_path: Path | None = None,
    block_copyleft: bool = True,
    allow_programbench_overlap: bool | None = None,
) -> GateResult:
    """Run the INGEST + LOCK gate, populating `manifest.source`.

    `local_path` (offline) takes precedence over cloning; otherwise clone `repo_url`
    (default `https://github.com/{repo}`) at `head_sha` into `work_dir`.
    `allow_programbench_overlap=None` (the call sites' default) defers to the LhConfig key,
    so the guard is operable without threading a new param through cli/api/orchestrator.
    """
    # ---- ProgramBench guard (ADR-0038): refuse official upstreams BEFORE any clone work.
    guard_ok, guard_reason = check_not_programbench(repo_url or repo, context=f"ingest {repo}")
    if not guard_ok:
        if allow_programbench_overlap is None:
            from ..config import LhConfig
            allow_programbench_overlap = LhConfig.load().allow_programbench_overlap
        if not allow_programbench_overlap:
            return GateResult("fail", "repo is in official ProgramBench (guard)",
                              {"guard": guard_reason})
        manifest.notes.append(f"programbench-guard OVERRIDDEN (allow_programbench_overlap): {guard_reason}")
    elif "near-miss" in guard_reason:
        # Lookalike name (astaxie/bat vs sharkdp/bat class): allowed, but keep the warning
        # on the record so a human skimming the run catches an accidental pick.
        if guard_reason not in manifest.notes:
            manifest.notes.append(guard_reason)

    if local_path is not None:
        root = Path(local_path)
        if not root.exists():
            return GateResult("fail", f"local_path does not exist: {root}")
    else:
        url = repo_url or f"https://github.com/{repo}"
        try:
            root = clone_at_sha(url, head_sha, Path(work_dir) / "source")
        except subprocess.CalledProcessError as e:
            return GateResult("fail", f"clone/checkout failed: {e.stderr or e}".strip())
        except subprocess.TimeoutExpired:
            # A wedged/too-slow clone drops THIS repo cleanly so the farm moves on — it never
            # hangs the synchronous ingest of the repos behind it (the wasm-tools-stall fix).
            return GateResult("fail", f"clone timed out after {CLONE_TIMEOUT}s (repo too large/slow "
                                      f"— raise PROGRAMSMITH_CLONE_TIMEOUT to retry)")
        repo_url = url

    primary, lang_counts = detect_language(root)
    build_systems = detect_build_systems(root)
    test_frameworks = detect_test_frameworks(root, build_systems)
    lic_name, lic_class, _lic_file = classify_license(root)
    size_files, size_loc = _count_loc(root, primary)
    has_cli, cli_marker = detect_cli_entrypoint(root, primary)

    copyleft = lic_class in ("strong-copyleft", "weak-copyleft")
    manifest.source = SourceInfo(
        repo=repo,
        pinned_sha=head_sha,
        repo_url=repo_url,
        license=lic_name,
        license_class=lic_class,
        copyleft_blocked=copyleft,
        primary_language=primary,
        build_systems=build_systems,
        test_frameworks=test_frameworks,
        size_files=size_files,
        size_loc=size_loc,
        clone_path=str(root),
        has_cli_entrypoint=has_cli,
        cli_entrypoint=cli_marker,
    )

    detail = {
        "language": primary, "language_counts": dict(lang_counts),
        "build_systems": build_systems, "test_frameworks": test_frameworks,
        "license": lic_name, "license_class": lic_class,
        "size_files": size_files, "size_loc": size_loc,
        "has_cli_entrypoint": has_cli,
    }
    # Keep the human-readable note for old dashboards while also persisting the structured fields.
    cli_note = (f"has_cli_entrypoint=true (marker: {cli_marker})" if has_cli
                else "has_cli_entrypoint=false (no main.go / src/main.rs / main.c / cmd/)")
    if cli_note not in manifest.notes:
        manifest.notes.append(cli_note)

    # License-class gate (ADR-0010): drop copyleft sources; unknown needs human classification.
    if block_copyleft and lic_class in ("strong-copyleft", "weak-copyleft"):
        return GateResult("fail", f"license {lic_name} is {lic_class}; dropped (ADR-0010)", detail)
    if lic_class == "unknown":
        return GateResult("fail", "license unrecognized; needs human classification", detail)
    if primary is None:
        return GateResult("fail", "no recognizable source language detected", detail)

    return GateResult("pass", f"locked {repo}@{head_sha[:10]} · {primary} · {lic_name}", detail)
