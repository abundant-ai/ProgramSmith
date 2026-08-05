"""Read-only file/directory browser for a run's working dir — the task-detail viewer.

Surfaces the generated Harbor task, the cloned source, sweep artifacts, and the run's JSON
manifests as a navigable tree with per-file preview. Everything is confined to the run directory:
paths are resolved and re-checked against the root so `..`/symlink traversal can't escape, large
files return metadata only (no 200 MB blob shipped to the browser), and binary files are flagged so
the client renders a download/hex affordance instead of garbage. Pure functions over a root Path so
the logic is unit-tested offline; the API layer just wires `tree()` / `read()` to endpoints.
"""

from __future__ import annotations

import base64
from pathlib import Path

MAX_PREVIEW_BYTES = 512 * 1024        # text/image preview cap (bigger ⇒ metadata only)
MAX_NODES = 6000                      # whole-tree node budget (truncates huge clones gracefully)
MAX_CHILDREN_PER_DIR = 800            # per-directory fan-out cap
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".idea", ".vscode"}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif"}
# extension → highlight language hint the frontend can use (best-effort; absent ⇒ plain text)
LANG_BY_EXT = {
    ".py": "python", ".rs": "rust", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "jsx", ".go": "go", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".java": "java", ".rb": "ruby", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".jsonl": "json",
    ".md": "markdown", ".markdown": "markdown", ".html": "html", ".css": "css", ".scss": "scss",
    ".sql": "sql", ".dockerfile": "dockerfile", ".cfg": "ini", ".ini": "ini", ".txt": "text",
    ".cast": "json", ".lock": "text", ".env": "bash", ".diff": "diff", ".patch": "diff",
}
SPECIAL_NAMES = {"Dockerfile": "dockerfile", "Makefile": "makefile", "Cargo.toml": "toml",
                 "task.toml": "toml", "go.mod": "go", "requirements.txt": "text"}

# Order: surface the things an operator wants first (the task, then artifacts, then the source clone).
_TOP_PRIORITY = {"task": 0, "probe-task": 1, "manifest.json": 2, "state.json": 3,
                 "task_matrix.json": 4, ".sweeps": 5, "source": 90}


def _safe_resolve(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, rejecting any path that escapes the root (`..`, abs, symlink)."""
    root = root.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes the run directory")
    return target


def _lang_for(p: Path) -> str | None:
    if p.name in SPECIAL_NAMES:
        return SPECIAL_NAMES[p.name]
    return LANG_BY_EXT.get(p.suffix.lower())


def _sort_key(p: Path, *, top: bool) -> tuple:
    pri = _TOP_PRIORITY.get(p.name, 50) if top else 50
    return (0 if p.is_dir() else 1, pri, p.name.lower())


def _node(root: Path, p: Path, budget: list[int], *, top: bool = False) -> dict:
    rel = str(p.relative_to(root))
    if p.is_dir():
        children: list[dict] = []
        truncated = False
        try:
            entries = sorted(p.iterdir(), key=lambda c: _sort_key(c, top=top))
        except OSError:
            entries = []
        for c in entries:
            if c.is_dir() and c.name in SKIP_DIRS:
                continue
            if len(children) >= MAX_CHILDREN_PER_DIR or budget[0] <= 0:
                truncated = True
                break
            budget[0] -= 1
            children.append(_node(root, c, budget))
        out = {"name": p.name, "path": rel, "type": "dir", "children": children}
        if truncated:
            out["truncated"] = True
        return out
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return {"name": p.name, "path": rel, "type": "file", "size": size, "lang": _lang_for(p)}


def tree(root: Path, subpath: str = "") -> dict:
    """A node tree rooted at `root/subpath` (run dir by default). Directories first, the generated
    task + artifacts ordered ahead of the bulky source clone. Truncates past MAX_NODES with a flag."""
    base = _safe_resolve(root, subpath) if subpath else root.resolve()
    if not base.exists():
        return {"name": base.name, "path": subpath, "type": "dir", "children": [], "missing": True}
    budget = [MAX_NODES]
    return _node(root.resolve(), base, budget, top=not subpath)


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    # high ratio of non-text bytes ⇒ binary
    text_chars = bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b"
    nontext = sum(1 for b in data[:4096] if b not in text_chars)
    return nontext / max(1, len(data[:4096])) > 0.30


def read(root: Path, rel: str) -> dict:
    """Read a single file for preview. Returns one of:
      {kind: "text", content, lang, size, path}
      {kind: "image", data_uri, size, path}          (small images, inlined)
      {kind: "binary", size, path}                   (no preview)
      {kind: "too_large", size, path}                (over the preview cap)
    Always includes `name`, `path`, `size`. Raises ValueError on traversal, FileNotFoundError on miss.
    """
    target = _safe_resolve(root, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel)
    size = target.stat().st_size
    common = {"name": target.name, "path": rel, "size": size}
    ext = target.suffix.lower()
    if ext in IMAGE_EXTS:
        if size > MAX_PREVIEW_BYTES:
            return {**common, "kind": "too_large"}
        mime = "svg+xml" if ext == ".svg" else ext.lstrip(".").replace("jpg", "jpeg")
        data = base64.b64encode(target.read_bytes()).decode("ascii")
        return {**common, "kind": "image", "data_uri": f"data:image/{mime};base64,{data}"}
    if size > MAX_PREVIEW_BYTES:
        return {**common, "kind": "too_large"}
    raw = target.read_bytes()
    if _looks_binary(raw):
        return {**common, "kind": "binary"}
    return {**common, "kind": "text", "lang": _lang_for(target),
            "content": raw.decode("utf-8", errors="replace")}
