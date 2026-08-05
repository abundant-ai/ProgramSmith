"""Shared oracle-pair build helpers (vendored from programbench-farm/_build_helpers.py).

Common pattern: clone upstream -> apply one-line version-string-suffix patch
-> build TWO byte-distinct binaries (oracle + prebuilt) -> verify their
.text sections diverge -> hand control back to the caller, which calls
build_task(...) with the case list.

Adaptations vs the farm (invariant #3, minimal): the module-global NONPB_ROOT
(/tmp/non-pb, created at import) becomes an explicit `workspace` parameter —
the pipeline's ORACLE cell drives these from a per-run capture.py, and an
import-time mkdir of a shared /tmp path is a cross-run collision. SystemExit
becomes RuntimeError: these run IN-PROCESS in the orchestrator (SystemExit is
a BaseException that `except Exception` won't catch, so it would kill the
fleet driver, not fail the run). Messages kept verbatim. `_text_section_sha`
gains objcopy fallbacks (pure-Python ELF parse, then whole-file hash) because
the build host may be macOS with no binutils — the farm solved this with a
.build-shims objcopy shim (SUBAGENT_PLAYBOOK); the pure-Python parse is that
shim folded in.

All build functions are idempotent: if <workspace>/<tool>-{oracle,prebuilt}
already exist and are runnable, they short-circuit. Pass force=True to rebuild.
"""
from __future__ import annotations
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from .guard import check_not_programbench


def _run(cmd, *, cwd=None, env=None, check=True):
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}", flush=True)
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=check,
        shell=isinstance(cmd, str),
    )


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _elf_text_section(data: bytes) -> bytes | None:
    """Pure-Python extraction of the .text section from a 64-bit little-endian ELF (the only
    shape the farm ships: linux/amd64 oracles). None when `data` isn't such an ELF — callers
    fall through to the whole-file hash. Replaces the farm's .build-shims objcopy shim."""
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    try:
        e_shoff, = struct.unpack_from("<Q", data, 0x28)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
        if e_shoff == 0 or e_shnum == 0 or e_shstrndx >= e_shnum:
            return None
        def sh(i: int) -> tuple[int, int, int, int]:
            base = e_shoff + i * e_shentsize
            name, _typ = struct.unpack_from("<II", data, base)
            offset, size = struct.unpack_from("<QQ", data, base + 0x18)
            return name, _typ, offset, size
        _, _, str_off, str_size = sh(e_shstrndx)
        strtab = data[str_off:str_off + str_size]
        for i in range(e_shnum):
            name_off, _typ, offset, size = sh(i)
            end = strtab.find(b"\x00", name_off)
            if strtab[name_off:end] == b".text":
                return data[offset:offset + size]
    except (struct.error, IndexError):
        return None
    return None


def _text_section_sha(p: Path) -> str:
    """sha256 of the binary's .text section — objcopy when available (byte-identical to the
    farm), pure-Python ELF parse otherwise, whole-file hash as the last resort (non-ELF oracle
    on a non-Linux host; the anti-cheat comparison then simply never false-rejects, because an
    agent's objcopy'd .text can't equal a whole-file hash)."""
    if shutil.which("objcopy"):
        out = Path(tempfile.mkstemp(prefix=p.name + ".text.", suffix=".bin")[1])
        try:
            subprocess.run(
                ["objcopy", "-O", "binary", "--only-section=.text", str(p), str(out)],
                check=True,
            )
            return _sha256(out)
        finally:
            out.unlink(missing_ok=True)
    data = p.read_bytes()
    text = _elf_text_section(data)
    return hashlib.sha256(text if text is not None else data).hexdigest()


def assert_text_distinct(oracle: Path, prebuilt: Path):
    """Anti-cheat layer 5 rejects identical .text sections (HANDOFF.md §7.5)."""
    a = _text_section_sha(Path(oracle))
    b = _text_section_sha(Path(prebuilt))
    if a == b:
        raise RuntimeError(
            f"FATAL: oracle and prebuilt .text sections are identical "
            f"(sha256={a[:16]}). Use different compile flags for the two builds."
        )
    print(f"  .text sha256: oracle={a[:16]}  prebuilt={b[:16]}  (distinct ✓)")


def shallow_clone(repo: str, tag_or_commit: str, dst: Path, *, force: bool = False):
    """git clone --depth 1 --branch <tag> <repo> <dst>.

    Falls back to a full clone + checkout if --branch fails (non-tag commit).
    """
    if dst.exists() and not force:
        print(f"  shallow_clone: {dst} already exists; skipping")
        return
    if dst.exists():
        shutil.rmtree(dst)
    try:
        _run(["git", "clone", "--depth", "1", "--branch", tag_or_commit, repo, str(dst)])
        return
    except subprocess.CalledProcessError:
        pass
    _run(["git", "clone", repo, str(dst)])
    _run(["git", "-C", str(dst), "checkout", tag_or_commit])


def patch_version_string(src: Path, find: str, replace: str, *, files: list[str]):
    """Apply a one-line version-string-suffix patch.

    HANDOFF.md §4 step 2: a single-line `<ver>` → `<ver>-nonpb` sed is the
    ONLY patch allowed. Behavior patches are forbidden (reducible to
    `agent applies my known patch via sed -i`).
    """
    hits = 0
    already = 0
    for rel in files:
        p = src / rel
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if find in txt:
            p.write_text(txt.replace(find, replace, 1))
            hits += 1
            print(f"  patched version string in {rel}")
        elif replace in txt:
            already += 1
            print(f"  patch already applied in {rel}")
    if hits == 0 and already == 0:
        raise RuntimeError(
            f"FATAL: version-string find={find!r} not found in any of {files}; "
            f"check upstream layout"
        )


def ensure_oracle_pair(
    *,
    tool: str,
    repo: str,
    tag_or_commit: str,
    patch_find: str,
    patch_replace: str,
    patch_files: list[str],
    build_oracle: str,
    build_prebuilt: str,
    oracle_artifact: str,
    prebuilt_artifact: str,
    workspace: Path,
    extra_env: dict | None = None,
    force: bool = False,
):
    """Drives clone → patch → build oracle → build prebuilt → assert distinct.

    Returns (oracle_path, prebuilt_path, src_dir). All artifacts live under `workspace`
    (the farm's /tmp/non-pb, now caller-owned). build_oracle / build_prebuilt are shell
    strings run from the src dir.
    """
    slug = f"implement-{tool}"
    ok, why = check_not_programbench(repo, context=f"ensure_oracle_pair tool={tool}", slug=slug)
    if not ok:
        raise RuntimeError(f"FATAL: {why}")
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    src = workspace / f"{tool}-src"
    oracle = workspace / f"{tool}-oracle"
    prebuilt = workspace / f"{tool}-prebuilt"

    if not force and oracle.exists() and prebuilt.exists():
        print(f"  ensure_oracle_pair[{tool}]: cached at {oracle}/{prebuilt}")
        assert_text_distinct(oracle, prebuilt)
        return oracle, prebuilt, src

    shallow_clone(repo, tag_or_commit, src, force=force)
    patch_version_string(src, patch_find, patch_replace, files=patch_files)

    env = os.environ.copy()
    env.setdefault("CARGO_NET_OFFLINE", "false")
    if extra_env:
        env.update(extra_env)
    _run(build_oracle, cwd=str(src), env=env)
    src_oracle = src / oracle_artifact
    shutil.copy(src_oracle, oracle)
    oracle.chmod(0o755)

    _run(build_prebuilt, cwd=str(src), env=env)
    src_prebuilt = src / prebuilt_artifact
    shutil.copy(src_prebuilt, prebuilt)
    prebuilt.chmod(0o755)

    assert_text_distinct(oracle, prebuilt)
    return oracle, prebuilt, src


def collect_docs(oracle: Path, *, help_arg: str = "--help",
                 version_arg: str = "--version") -> tuple[bytes, bytes]:
    """Capture <tool> --help and --version output from the oracle."""
    help_b = subprocess.run(
        [str(oracle), help_arg], capture_output=True
    ).stdout
    if not help_b:
        help_b = subprocess.run(
            [str(oracle), "-h"], capture_output=True
        ).stdout or b"(no help available)\n"
    version_b = subprocess.run(
        [str(oracle), version_arg], capture_output=True
    ).stdout
    if not version_b:
        version_b = b"(no version available)\n"
    return help_b, version_b
