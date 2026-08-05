"""Free source inspection before the paid TASK MATRIX cell.

The deterministic layer records facts and warnings; it does not guess product semantics from a
dependency or README keyword. Only hard incompatibilities are rejected here. The same inspection
builds a bounded evidence dossier for TASK MATRIX and a high-precision discovery recommendation
for farms deciding which public repositories are worth a paid semantic review.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .manifest import Manifest

TaskMatrixProfile = Literal["full", "draft"]

_SUPPORTED_LANGUAGES = {"Go": "go", "Rust": "rust", "C": "c", "C++": "cpp"}
_SKIP_DIRS = {".git", "node_modules", "target", "build", ".venv", "venv", "dist", "vendor"}
_ENTRYPOINT_SKIP_DIRS = {
    "test", "tests", "example", "examples", "demo", "demos", "benchmark", "benchmarks",
    "fixture", "fixtures", "sample", "samples", "testdata", "fuzz", "fuzzing",
}

_TUI_MARKERS = (
    "bubbletea", "charmbracelet/bubbles", "charmbracelet/lipgloss", "tcell", "tview",
    "ratatui", "crossterm", "ncurses", "notcurses", "cursive", "termion",
)
_INTERACTIVE_MARKERS = (
    "terminal user interface", "terminal ui", "interactive terminal", "full-screen terminal",
    "full screen terminal", " tui ", "ncurses",
)
_REMOTE_MARKERS = (
    "api client", "client for the api", "client for the ", "cloud cli", "cloud command line",
    "rest api", "graphql api", "requires authentication", "api token", "oauth token",
    "access token", "login to your account", "remote service", "remote machines",
    "remote compute cloud", "command-line client", "orchestrator command line client",
    "kubernetes cluster", "aws s3", "cloudwatch", "discord client", "gmail api", "app password",
    "http requests", "credentials via", "openai's api", "openai api", "fetch web page",
)
_LOCAL_SURFACE_MARKERS = (
    "stdin", "standard input", "offline", "formatter", "parser", "convert", "transform",
    "parse", "validate", "encode", "decode", "query files", "search files", "grep", "diff", "archive", "compress", "--json",
    "json output", "non-interactive", "batch mode",
)
_NETWORK_ESCAPE_MARKERS = (
    "stdin", "standard input", "offline", "parser", "parse", "convert", "transform",
    "validate", "encode", "decode",
)
_DECISIVE_REMOTE_MARKERS = (
    "api client", "rest api", "graphql api", "requires authentication", "api token",
    "oauth token", "access token", "login to your account", "remote machines",
    "remote compute cloud", "orchestrator command line client", "kubernetes cluster",
    "aws s3", "cloudwatch", "discord client", "gmail api", "app password", "http requests", "credentials via",
    "openai's api", "openai api", "fetch web page",
)
_ALWAYS_REMOTE_MARKERS = (
    "requires authentication", "api token", "oauth token", "access token",
    "login to your account", "remote machines", "remote compute cloud", "aws s3",
    "cloudwatch", "discord client", "gmail api", "app password", "http requests", "credentials via",
    "openai's api", "openai api", "fetch web page",
)
_HARD_TUI_MARKERS = (
    "bubbletea", "charmbracelet/bubbles", "tcell", "tview", "ratatui", "ncurses",
    "notcurses", "cursive", "termion",
)


class SourceScreenResult(BaseModel):
    profile: TaskMatrixProfile
    eligible: bool
    reason: str
    checks: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SourceReviewResult(BaseModel):
    """Whether a source is worth a paid TASK MATRIX review—not whether a task is viable."""

    decision: Literal["review", "low_confidence", "reject"]
    score: int
    reasons: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


_OFFLINE_SURFACE_MARKERS = (
    "stdin", "standard input", "csv", "json", "yaml", "toml", "xml", "markdown",
    "formatter", "format ", "parser", "parse ", "convert", "transform", "validate",
    "encode", "decode", "query", "search", "grep", "diff", "archive", "compress",
    "hash", "checksum", "calculator", "template", "render", "filter", "sort", "join",
    "lint", "file format", "log analyzer", "static analysis", "offline", "batch mode",
    "non-interactive", "json output", "csv output", "stdout",
)

_DOSSIER_SKIP_DIRS = _SKIP_DIRS | {".github", ".idea", ".vscode", "coverage"}
_MANIFEST_NAMES = (
    "Cargo.toml", "go.mod", "Makefile", "CMakeLists.txt", "meson.build", "build.zig",
)


def task_matrix_profile(manifest: Manifest) -> TaskMatrixProfile:
    return "draft" if manifest.pipeline_mode == "draft" else "full"


def _iter_code(root: Path, suffixes: set[str]):
    for path in root.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in suffixes
            and not any(part in _SKIP_DIRS for part in path.parts)
        ):
            yield path


def detect_cli_entrypoint(root: Path, language: str | None) -> tuple[bool, str]:
    """Find a real executable entrypoint, not merely a repository name/topic that says "CLI"."""
    if not root.is_dir():
        return False, "locked checkout missing"

    if language == "Go":
        for path in _iter_code(root, {".go"}):
            if any(part.lower() in _ENTRYPOINT_SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
                continue
            try:
                head = path.read_text(errors="ignore")[:80_000]
            except OSError:
                continue
            if re.search(r"(?m)^\s*package\s+main\b", head) and re.search(
                r"\bfunc\s+main\s*\(", head
            ):
                return True, str(path.relative_to(root))
    elif language == "Rust":
        for candidate in (root / "src" / "main.rs",):
            if candidate.is_file():
                return True, str(candidate.relative_to(root))
        bin_dir = root / "src" / "bin"
        if bin_dir.is_dir() and any(bin_dir.glob("*.rs")):
            return True, "src/bin/"
        for cargo in root.rglob("Cargo.toml"):
            if any(part in _SKIP_DIRS for part in cargo.parts):
                continue
            if any(
                part.lower() in _ENTRYPOINT_SKIP_DIRS
                for part in cargo.relative_to(root).parts[:-1]
            ):
                continue
            try:
                if "[[bin]]" in cargo.read_text(errors="ignore"):
                    return True, str(cargo.relative_to(root)) + " [[bin]]"
            except OSError:
                pass
    elif language in {"C", "C++"}:
        suffixes = {".c"} if language == "C" else {".cc", ".cpp", ".cxx", ".c++"}
        main_re = re.compile(r"\b(?:int|void|auto)\s+main\s*\(")
        for path in _iter_code(root, suffixes):
            if any(part.lower() in _ENTRYPOINT_SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
                continue
            try:
                if main_re.search(path.read_text(errors="ignore")[:200_000]):
                    return True, str(path.relative_to(root))
            except OSError:
                pass
    return False, "no executable main entrypoint found"


def _read_text(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(path.read_text(errors="ignore")[:120_000].lower())
        except OSError:
            pass
    return "\n".join(chunks)


def _readme_text(root: Path) -> str:
    paths: list[Path] = []
    for pattern in ("README*", "readme*"):
        paths.extend(sorted(root.glob(pattern))[:2])
    return _read_text(paths)


def _dependency_text(root: Path) -> str:
    paths: list[Path] = []
    for name in ("go.mod", "Cargo.toml"):
        path = root / name
        if path.is_file():
            paths.append(path)
    return _read_text(paths)


def _strong_surface_mismatch(root: Path) -> tuple[str | None, dict[str, object]]:
    readme = _readme_text(root)
    dependencies = _dependency_text(root)
    tui_deps = sorted(marker for marker in _TUI_MARKERS if marker in dependencies)
    interactive = sorted(marker.strip() for marker in _INTERACTIVE_MARKERS if marker in readme)
    remote = sorted(marker for marker in _REMOTE_MARKERS if marker in readme)
    local = sorted(marker for marker in _LOCAL_SURFACE_MARKERS if marker in readme)
    network_escape = sorted(marker for marker in _NETWORK_ESCAPE_MARKERS if marker in readme)
    hard_tui = sorted(marker for marker in _HARD_TUI_MARKERS if marker in dependencies)
    details: dict[str, object] = {
        "tui_markers": tui_deps,
        "interactive_markers": interactive,
        "remote_markers": remote,
        "local_surface_markers": local,
        "network_escape_markers": network_escape,
    }
    # Full-screen frameworks are structural evidence, not mere prose. Permit them only when the
    # README explicitly documents a non-interactive/batch path; generic parser/JSON wording does
    # not turn a TUI into a black-box CLI task.
    has_batch_mode = "non-interactive" in local or "batch mode" in local
    # One mention can be a comparison/reference (e.g. "unlike fx, an interactive terminal tool").
    # Two independent product signals are strong enough to classify an undocumented TUI.
    if len(interactive) >= 2 and not has_batch_mode:
        return "interactive/TUI-only source with no documented batch output mode", details
    if hard_tui and not has_batch_mode:
        return "interactive/TUI-only source with no documented batch output mode", details
    decisive_remote = any(marker in _DECISIVE_REMOTE_MARKERS for marker in remote)
    always_remote = any(marker in _ALWAYS_REMOTE_MARKERS for marker in remote)
    if always_remote or ((len(remote) >= 2 or decisive_remote) and not network_escape):
        return "remote-service client with no documented offline transformation surface", details
    return None, details


def _bounded_file(path: Path, limit: int) -> str:
    try:
        text = path.read_text(errors="ignore").replace("\x00", "")
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… ({len(text) - limit:,} characters omitted)"


def _repo_file_overview(root: Path, limit: int = 100) -> list[str]:
    """A stable, shallow-enough source tree that exposes commands/docs without prompt bloat."""
    found: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root)).lower()):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _DOSSIER_SKIP_DIRS for part in rel.parts):
            continue
        if len(rel.parts) <= 3 or any(part.lower() in {"cmd", "command", "commands", "docs", "doc"}
                                      for part in rel.parts):
            found.append(str(rel))
        if len(found) >= limit:
            break
    return found


def build_source_dossier(manifest: Manifest) -> dict[str, object]:
    """Build bounded, path-labelled evidence from the locked checkout for TASK MATRIX.

    No code is executed. The dossier intentionally favors product documentation, executable entry
    code, package manifests, and command/document filenames—the evidence needed to distinguish an
    offline CLI surface from a library, TUI, or remote-only client.
    """
    source = manifest.source
    if source is None:
        return {"error": "source metadata missing", "evidence": []}
    checkout = Path(source.clone_path) if source.clone_path else None
    if checkout is None or not checkout.is_dir():
        return {
            "repo": source.repo,
            "error": "locked checkout missing",
            "evidence": [],
        }

    has_cli, entrypoint = detect_cli_entrypoint(checkout, source.primary_language)
    evidence: list[dict[str, str]] = []
    readmes: list[Path] = []
    for pattern in ("README*", "readme*"):
        readmes.extend(sorted(checkout.glob(pattern))[:1])
    seen: set[Path] = set()
    for path in readmes:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        content = _bounded_file(path, 16_000)
        if content:
            evidence.append({"path": str(path.relative_to(checkout)), "kind": "readme", "content": content})

    if has_cli:
        entry = checkout / entrypoint.split(" ", 1)[0]
        if entry.is_file():
            content = _bounded_file(entry, 8_000)
            if content:
                evidence.append({"path": str(entry.relative_to(checkout)), "kind": "entrypoint", "content": content})

    for name in _MANIFEST_NAMES:
        path = checkout / name
        if path.is_file():
            content = _bounded_file(path, 4_000)
            if content:
                evidence.append({"path": name, "kind": "build_manifest", "content": content})
        if sum(len(str(item.get("content", ""))) for item in evidence) >= 30_000:
            break

    mismatch, surface_checks = _strong_surface_mismatch(checkout)
    warnings = [mismatch] if mismatch else []
    return {
        "repo": source.repo,
        "pinned_sha": source.pinned_sha,
        "language": source.primary_language,
        "size_loc": source.size_loc,
        "entrypoint_detected": has_cli,
        "entrypoint": entrypoint,
        "surface_signals": surface_checks,
        "warnings": warnings,
        "file_overview": _repo_file_overview(checkout),
        "evidence": evidence,
    }


def recommend_source_review(manifest: Manifest) -> SourceReviewResult:
    """High-precision admission to paid semantic review for automated source discovery.

    A low-confidence result is not a task rejection: an operator may still run it explicitly. This
    function exists so broad GitHub crawls do not flood the Runs view or spend model calls on generic
    `cli` topic matches with no documented offline functional surface.
    """
    screen = screen_source(manifest)
    if not screen.eligible:
        return SourceReviewResult(
            decision="reject", score=0, reasons=[screen.reason], warnings=screen.warnings,
        )
    source = manifest.source
    root = Path(source.clone_path) if source and source.clone_path else None
    readme = _readme_text(root) if root and root.is_dir() else ""
    positive = sorted({marker.strip() for marker in _OFFLINE_SURFACE_MARKERS if marker in readme})
    has_cli = bool(screen.checks.get("has_cli_entrypoint"))
    score = (3 if has_cli else 0) + min(6, len(positive))
    if screen.warnings:
        score -= 1
    reasons: list[str] = []
    if has_cli:
        reasons.append(f"executable entrypoint: {screen.checks.get('cli_entrypoint')}")
    if positive:
        reasons.append("documented offline/data surface: " + ", ".join(positive[:8]))
    if not has_cli:
        reasons.append("no executable entrypoint detected; requires semantic inspection")
    if len(positive) < 2:
        reasons.append("fewer than two documented offline functional signals")
    decision: Literal["review", "low_confidence", "reject"] = (
        "review" if has_cli and len(positive) >= 2 else "low_confidence"
    )
    return SourceReviewResult(
        decision=decision,
        score=max(0, score),
        reasons=reasons,
        positive_signals=positive,
        warnings=screen.warnings,
    )


def screen_source(manifest: Manifest, root: str | Path | None = None) -> SourceScreenResult:
    profile = task_matrix_profile(manifest)
    source = manifest.source
    if source is None:
        return SourceScreenResult(
            profile=profile,
            eligible=False,
            reason="source metadata is missing",
            checks={"source_present": False},
        )

    language = source.primary_language
    loc = int(source.size_loc or 0)
    checks: dict[str, object] = {"language": language, "size_loc": loc}
    if language not in _SUPPORTED_LANGUAGES:
        return SourceScreenResult(
            profile=profile,
            eligible=False,
            reason=f"unsupported upstream language {language or 'unknown'}; expected Go, Rust, C, or C++",
            checks=checks,
        )

    minimum, maximum = ((500, 120_000) if profile == "draft" else (5_000, 80_000))
    checks.update({"minimum_loc": minimum, "maximum_loc": maximum})
    warnings: list[str] = []
    if loc < minimum:
        warnings.append(f"source has {loc:,} LOC; below the {profile} profile guideline of {minimum:,}")
    if loc > maximum:
        warnings.append(f"source has {loc:,} LOC; above the {profile} profile guideline of {maximum:,}")

    checkout_value = root or source.clone_path
    if not checkout_value:
        return SourceScreenResult(
            profile=profile,
            eligible=False,
            reason="locked source checkout path is missing",
            checks=checks,
        )
    checkout = Path(checkout_value)
    has_cli, marker = detect_cli_entrypoint(checkout, language)
    checks.update({"has_cli_entrypoint": has_cli, "cli_entrypoint": marker})
    if not has_cli:
        warnings.append(marker)

    mismatch, surface_checks = _strong_surface_mismatch(checkout)
    checks.update(surface_checks)
    if mismatch:
        warnings.append(mismatch)
    return SourceScreenResult(
        profile=profile,
        eligible=True,
        reason=(
            f"{profile} source is eligible for evidence-based task review"
            if not warnings else
            f"{profile} source requires semantic review ({len(warnings)} warning(s))"
        ),
        checks=checks,
        warnings=warnings,
    )
