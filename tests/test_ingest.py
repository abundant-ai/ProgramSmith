"""Offline tests for the INGEST + LOCK gate: license classification, detection, and the
copyleft gate. The real minpack@SHA clone is exercised as a manual dogfood, not a network test.
"""

from pathlib import Path

import pytest

from programsmith.gates.ingest import (
    classify_license,
    detect_build_systems,
    detect_language,
    ingest,
)
from programsmith.manifest import Manifest

MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person ..."
GPL = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n ..."
AGPL = "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3 ..."
LGPL = "GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1 ..."
BSD = "Redistribution and use in source and binary forms, with or without modification ..."
MINPACK = ("Minpack Copyright Notice (1999) University of Chicago.  All rights reserved.\n"
           "Redistribution and use in source and binary forms ...")


def _make_repo(tmp_path: Path, license_text: str, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "LICENSE.txt").write_text(license_text)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


@pytest.mark.parametrize(
    "text,expected_class",
    [(MIT, "permissive"), (BSD, "permissive"), (MINPACK, "permissive"),
     (GPL, "strong-copyleft"), (AGPL, "strong-copyleft"), (LGPL, "weak-copyleft")],
)
def test_classify_license(tmp_path, text, expected_class):
    root = _make_repo(tmp_path, text, {})
    _name, klass, _file = classify_license(root)
    assert klass == expected_class


def test_classify_license_dual_and_unusual_permissive(tmp_path):
    """Real-repo licensing that used to false-drop during farm ingest: a DUAL-licensed project
    (permissive option + GPL COPYING) classifies PERMISSIVE — the usable path wins (rocksdb). The
    classifier scans ALL top-level license files (flac's COPYING.Xiph) and recognizes permissive-but-
    unusual texts (OpenLDAP/LMDB, libpng, the X11/awk-style grant). A GPL-only project still drops."""
    # rocksdb-style: a GPL COPYING must NOT win over the Apache license file
    rocks = tmp_path / "rocksdb"; rocks.mkdir()
    (rocks / "COPYING").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991 ...")
    (rocks / "LICENSE.Apache").write_text("Apache License\nVersion 2.0, January 2004 ...")
    assert classify_license(rocks)[1] == "permissive"
    # flac: license lives in COPYING.Xiph (BSD) — found by the glob, not the old fixed name list
    flac = tmp_path / "flac"; flac.mkdir()
    (flac / "COPYING.Xiph").write_text("Redistribution and use in source and binary forms ...")
    assert classify_license(flac)[1] == "permissive"
    # permissive-but-unusual texts that previously returned 'unrecognized'
    for i, txt in enumerate([
        "The OpenLDAP Public License\n\nRedistribution ...",                 # LMDB / openldap
        "This copy of the libpng notices ... PNG Reference Library License", # libpng
        "Permission to use, copy, modify, and distribute this software ...", # One True AWK (X11-style)
    ]):
        d = tmp_path / f"perm{i}"; d.mkdir()
        (d / "LICENSE").write_text(txt)
        assert classify_license(d)[1] == "permissive", txt[:32]
    # a GPL-only project still classifies copyleft (the drop policy is unchanged)
    gpl = tmp_path / "coreutils"; gpl.mkdir()
    (gpl / "COPYING").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007 ...")
    assert classify_license(gpl)[1] == "strong-copyleft"


def test_classify_license_farm_false_drops(tmp_path):
    """The six real-world INGEST false-drops this fix targets (verified against the live license texts):
    a permissive license that MENTIONS the GPL (Python PSF), a DUAL license in ONE file (mbedTLS
    Apache-OR-GPL, whose embedded GPL preamble mentions the 'Lesser' GPL), the 'Old MIT' grant
    (harfbuzz), a mixed-CASE filename (libxml2 `Copyright`), the zlib grant, and a grant that lives
    only in a source header's END-of-file notice (Lua `lua.h`) with no LICENSE file at all."""
    def mk(name: str, files: dict[str, str]):
        d = tmp_path / name; d.mkdir()
        for fn, txt in files.items():
            (d / fn).write_text(txt)
        return classify_license(d)

    # cpython PSF — text says "PYTHON SOFTWARE FOUNDATION LICENSE" AND "GPL-compatible"
    r = mk("cpython", {"LICENSE": "A. HISTORY OF THE SOFTWARE\nPYTHON SOFTWARE FOUNDATION LICENSE "
                       "VERSION 2\n... All Python licenses, unlike the GPL, let you distribute ..."})
    assert r[1] == "permissive" and r[0] == "PSF-2.0"

    # mbedTLS dual in one file — Apache text + GPL text (whose preamble names the 'Lesser' GPL)
    r = mk("mbedtls", {"LICENSE": "Mbed TLS files are provided under a dual Apache-2.0 OR "
                       "GPL-2.0-or-later license.\nApache License\nVersion 2.0, January 2004\n...\n"
                       "GNU GENERAL PUBLIC LICENSE\n... the GNU Lesser General Public License ..."})
    assert r[1] == "permissive"

    # harfbuzz "Old MIT" — "Permission is hereby granted, WITHOUT written agreement ..."
    r = mk("harfbuzz", {"COPYING": "HarfBuzz is licensed under the so-called Old MIT license.\n"
                        "Permission is hereby granted, without written agreement and without license "
                        "or royalty fees, to use, copy, modify, and distribute this software ..."})
    assert r[1] == "permissive"

    # libxml2 — license in a MIXED-CASE filename `Copyright` (must match case-insensitively)
    r = mk("libxml2", {"Copyright": "Except where otherwise noted ...\nPermission is hereby granted, "
                       "free of charge, to any person obtaining a copy of this software ..."})
    assert r[1] == "permissive" and r[2] == "Copyright"

    # zlib — distinctive grant, no literal "zlib license" string
    r = mk("zlib", {"LICENSE": "This software is provided 'as-is' ...\nPermission is granted to anyone "
                    "to use this software for any purpose, including commercial applications ..."})
    assert r[1] == "permissive" and r[0] == "Zlib"

    # SPDX tag (dual) resolves permissive
    r = mk("spdxdual", {"LICENSE": "SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later\n"})
    assert r[1] == "permissive"

    # Lua — NO license file; MIT grant ONLY in lua.h's end-of-file notice, and ~40 sibling .c files
    # (which merely say "See Copyright Notice in lua.h") must NOT crowd lua.h out of the bounded scan.
    tail = "x\n" * 500 + ("* Copyright (C) 1994 Lua.org, PUC-Rio.\n* Permission is hereby granted, "
                          "free of charge, to any person obtaining a copy of this software ...\n")
    files = {"README.md": "# Lua\nSee lua.h for the copyright notice.\n",
             "lua.h": "/* See Copyright Notice at the end of this file */\n" + tail}
    for n in ("lapi", "lcode", "ldo", "lgc", "llex", "lmem", "lobject", "lparser", "lstate",
              "lstring", "ltable", "ltm", "lvm", "lzio", "lauxlib", "lbaselib", "ldblib", "liolib"):
        files[f"{n}.c"] = "/* See Copyright Notice in lua.h */\nint x;\n"
        files[f"{n}.h"] = "/* See Copyright Notice in lua.h */\n"
    r = mk("lua", files)
    assert r[1] == "permissive" and r[2] == "lua.h"

    # a copyleft project with a stray 'BSD' mention in a source comment must STAY copyleft (the strict
    # fallback matcher must not leak a permissive verdict off a loose token)
    r = mk("gpl_bsd_mention", {"COPYING": "GNU GENERAL PUBLIC LICENSE\nVersion 3 ...",
                               "x.c": "/* works on BSD too */\nint x;\n"})
    assert r[1] == "strong-copyleft"


def test_detect_language_and_build_fortran(tmp_path):
    root = _make_repo(tmp_path, BSD, {
        "fpm.toml": "[package]\nname='x'\n",
        "meson.build": "project('x')\n",
        "src/minpack.f90": "module minpack\nend module\n",
        "src/lmdif.f90": "subroutine lmdif\nend\n",
    })
    primary, counts = detect_language(root)
    assert primary == "Fortran"
    assert counts["Fortran"] == 2
    builds = detect_build_systems(root)
    assert "fpm" in builds and "meson" in builds


def test_detect_language_rust(tmp_path):
    root = _make_repo(tmp_path, MIT, {
        "Cargo.toml": "[package]\nname='x'\n",
        "src/lib.rs": "pub fn f() {}\n",
    })
    primary, _ = detect_language(root)
    assert primary == "Rust"
    assert "cargo" in detect_build_systems(root)


def test_ingest_local_permissive_passes(tmp_path):
    root = _make_repo(tmp_path, MINPACK, {
        "fpm.toml": "[package]\nname='minpack'\n",
        "src/minpack.f90": "module minpack\nend module\n",
        "test/test_lmdif.f90": "program t\nend program\n",
    })
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "fortran-lang/minpack", "deadbeef", work_dir=tmp_path / "wd",
                 local_path=root)
    assert res.verdict == "pass"
    assert m.source is not None
    assert m.source.primary_language == "Fortran"
    assert m.source.license_class == "permissive"
    assert not m.source.copyleft_blocked
    assert "fpm" in m.source.build_systems


def test_ingest_blocks_copyleft(tmp_path):
    root = _make_repo(tmp_path, GPL, {
        "go.mod": "module x\n",
        "main.go": "package main\nfunc main(){}\n",
    })
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "some/gplrepo", "cafe", work_dir=tmp_path / "wd", local_path=root)
    assert res.verdict == "fail"
    assert "strong-copyleft" in res.reason
    assert m.source.copyleft_blocked  # populated even on a drop, for the record


def test_ingest_unknown_language_fails(tmp_path):
    root = _make_repo(tmp_path, MIT, {"README.md": "# docs only\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "x/y", "z", work_dir=tmp_path / "wd", local_path=root)
    assert res.verdict == "fail"


def test_ingest_clone_timeout_drops_cleanly(tmp_path, monkeypatch):
    """The scaling-bug guard: a wedged/too-slow clone must fail THIS repo cleanly (verdict 'fail')
    so a synchronous farm moves on to the next repo — never hang forever behind one big repo."""
    import subprocess

    from programsmith.gates import ingest as ing

    def _hang(url, sha, dest):
        raise subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=ing.CLONE_TIMEOUT)

    monkeypatch.setattr(ing, "clone_at_sha", _hang)
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ing.ingest(m, "big/monorepo", "deadbeef", work_dir=tmp_path / "wd")  # no local_path → clones
    assert res.verdict == "fail" and "timed out" in res.reason


def test_clone_uses_treeless_partial_clone(tmp_path, monkeypatch):
    """The clone must be a treeless partial clone (--filter=blob:none) — the difference between a
    few-MB fetch and a hundreds-of-MB full-history clone that stalls the farm."""
    from programsmith.gates import ingest as ing

    calls: list[list[str]] = []

    def _fake_git(args, cwd=None, *, timeout=None):
        calls.append(args)
        import subprocess
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ing, "_git", _fake_git)
    ing.clone_at_sha("https://github.com/o/n", "abc123", tmp_path / "src")
    clone = next(a for a in calls if a and a[0] == "clone")
    assert "--filter=blob:none" in clone


# ---- ProgramBench guard + CLI-entrypoint advisory (ADR-0038) --------------------------

def test_ingest_blocks_official_programbench_repo(tmp_path):
    root = _make_repo(tmp_path, MIT, {"Cargo.toml": "[package]\nname='bat'\n",
                                      "src/main.rs": "fn main() {}\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "sharkdp/bat", "cafe", work_dir=tmp_path / "wd", local_path=root,
                 allow_programbench_overlap=False)
    assert res.verdict == "fail"
    assert res.reason == "repo is in official ProgramBench (guard)"
    assert "sharkdp/bat" in res.detail["guard"]


def test_ingest_guard_default_reads_config(tmp_path, monkeypatch):
    # allow_programbench_overlap=None defers to LhConfig (default False) — point the config at
    # a nonexistent tmp file so the operator's real .programsmith/config.json is never consulted
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))
    root = _make_repo(tmp_path, MIT, {"Cargo.toml": "x\n", "src/main.rs": "fn main() {}\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "sharkdp/bat", "cafe", work_dir=tmp_path / "wd", local_path=root)
    assert res.verdict == "fail" and "guard" in res.reason


def test_ingest_overlap_override_passes_with_note(tmp_path):
    root = _make_repo(tmp_path, MIT, {"Cargo.toml": "x\n", "src/main.rs": "fn main() {}\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "sharkdp/bat", "cafe", work_dir=tmp_path / "wd", local_path=root,
                 allow_programbench_overlap=True)
    assert res.verdict == "pass"
    assert any("programbench-guard OVERRIDDEN" in n for n in m.notes)


def test_ingest_near_miss_lookalike_noted_but_passes(tmp_path):
    # someoneelse/bat is NOT official, but shares the repo name with sharkdp/bat + astaxie/bat —
    # allowed, with the lookalike warning on the record (HANDOFF near-miss rule)
    root = _make_repo(tmp_path, MIT, {"go.mod": "module bat\n", "main.go": "package main\nfunc main(){}\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "someoneelse/bat", "cafe", work_dir=tmp_path / "wd", local_path=root,
                 allow_programbench_overlap=False)
    assert res.verdict == "pass"
    assert any("near-miss" in n for n in m.notes)


def test_ingest_records_cli_entrypoint_advisory(tmp_path):
    # main.go marker → true; docs-only tree → false. Advisory only: never a verdict driver.
    cli = _make_repo(tmp_path, MIT, {"go.mod": "module t\n", "main.go": "package main\nfunc main(){}\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "example/clitool", "cafe", work_dir=tmp_path / "wd", local_path=cli,
                 allow_programbench_overlap=False)
    assert res.verdict == "pass"
    assert res.detail["has_cli_entrypoint"] is True
    assert m.source.has_cli_entrypoint is True
    assert m.source.cli_entrypoint
    assert any(n.startswith("has_cli_entrypoint=true") for n in m.notes)

    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "LICENSE.txt").write_text(MIT)
    (lib / "Cargo.toml").write_text("[package]\nname='x'\n")
    (lib / "src").mkdir(); (lib / "src" / "lib.rs").write_text("pub fn f() {}\n")
    m2 = Manifest(run_id="r2", task_identity="src:def")
    res2 = ingest(m2, "example/libonly", "cafe", work_dir=tmp_path / "wd2", local_path=lib,
                  allow_programbench_overlap=False)
    assert res2.verdict == "pass"
    assert res2.detail["has_cli_entrypoint"] is False
    assert m2.source.has_cli_entrypoint is False
    assert m2.source.cli_entrypoint == "no executable main entrypoint found"
    assert any(n.startswith("has_cli_entrypoint=false") for n in m2.notes)


def test_ingest_cmd_dir_counts_as_cli_marker(tmp_path):
    root = _make_repo(tmp_path, MIT, {"go.mod": "module t\n",
                                      "cmd/tool/main.go": "package main\nfunc main(){}\n"})
    m = Manifest(run_id="r", task_identity="src:abc")
    res = ingest(m, "example/multicmd", "cafe", work_dir=tmp_path / "wd", local_path=root,
                 allow_programbench_overlap=False)
    assert res.detail["has_cli_entrypoint"] is True
    assert any("cmd/" in n for n in m.notes if n.startswith("has_cli_entrypoint"))
