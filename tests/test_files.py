"""Tests for the run file/directory browser (ui/files.py) — tree, preview, and traversal safety."""

import base64

import pytest

from programsmith.ui import files as fb


def _run(tmp_path):
    root = tmp_path / "run"
    (root / "task" / "demo" / "tests").mkdir(parents=True)
    (root / "task" / "demo" / "task.toml").write_text("[task]\nname='demo'\n")
    (root / "task" / "demo" / "instruction.md").write_text("# Rewrite minpack\nDo the thing.\n")
    (root / "task" / "demo" / "tests" / "test.sh").write_text("#!/bin/bash\necho ok\n")
    (root / "source").mkdir()
    (root / "source" / "main.c").write_text("int main(){return 0;}\n")
    (root / "manifest.json").write_text('{"slug":"demo"}')
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main")
    return root


def test_tree_orders_task_before_source_and_skips_git(tmp_path):
    root = _run(tmp_path)
    t = fb.tree(root)
    names = [c["name"] for c in t["children"]]
    assert ".git" not in names                       # skipped
    assert names.index("task") < names.index("source")  # task surfaced first
    # dirs precede files at the top level
    assert names.index("task") < names.index("manifest.json")


def test_tree_is_recursive_with_lang_hints(tmp_path):
    root = _run(tmp_path)
    t = fb.tree(root)
    task = next(c for c in t["children"] if c["name"] == "task")
    demo = task["children"][0]
    files = {c["name"]: c for c in demo["children"]}
    assert files["task.toml"]["lang"] == "toml"
    assert files["instruction.md"]["lang"] == "markdown"
    assert files["tests"]["type"] == "dir"


def test_tree_subpath_scopes(tmp_path):
    root = _run(tmp_path)
    t = fb.tree(root, "task/demo")
    assert t["name"] == "demo" and {c["name"] for c in t["children"]} >= {"task.toml", "tests"}


def test_read_text_returns_content_and_lang(tmp_path):
    root = _run(tmp_path)
    out = fb.read(root, "task/demo/instruction.md")
    assert out["kind"] == "text" and out["lang"] == "markdown"
    assert "Rewrite minpack" in out["content"]


def test_read_image_inlines_base64(tmp_path):
    root = _run(tmp_path)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    (root / "task" / "demo" / "pic.png").write_bytes(png)
    out = fb.read(root, "task/demo/pic.png")
    assert out["kind"] == "image" and out["data_uri"].startswith("data:image/png;base64,")


def test_read_binary_flagged_no_preview(tmp_path):
    root = _run(tmp_path)
    (root / "task" / "demo" / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary\x00data")
    out = fb.read(root, "task/demo/blob.bin")
    assert out["kind"] == "binary" and "content" not in out


def test_read_too_large_returns_metadata_only(tmp_path):
    root = _run(tmp_path)
    big = root / "task" / "demo" / "big.txt"
    big.write_text("x" * (fb.MAX_PREVIEW_BYTES + 10))
    out = fb.read(root, "task/demo/big.txt")
    assert out["kind"] == "too_large" and "content" not in out


def test_traversal_is_rejected(tmp_path):
    root = _run(tmp_path)
    (tmp_path / "secret.txt").write_text("nope")
    with pytest.raises(ValueError):
        fb.read(root, "../secret.txt")
    with pytest.raises(ValueError):
        fb.tree(root, "../")


def test_read_missing_file(tmp_path):
    root = _run(tmp_path)
    with pytest.raises(FileNotFoundError):
        fb.read(root, "task/demo/nope.txt")
