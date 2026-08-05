"""Deterministic, zero-model-cost source screening for full and draft task profiles."""

from pathlib import Path

from programsmith.manifest import Manifest, SourceInfo
from programsmith.source_screen import (
    build_source_dossier,
    detect_cli_entrypoint,
    recommend_source_review,
    screen_source,
)


def _manifest(root: Path, *, loc: int = 1_200, mode: str = "draft", language: str = "Go") -> Manifest:
    manifest = Manifest(run_id="r", task_identity="src:r", pipeline_mode=mode)
    manifest.source = SourceInfo(
        repo="owner/tool",
        pinned_sha="abc123",
        primary_language=language,
        size_loc=loc,
        clone_path=str(root),
    )
    return manifest


def _go_cli(root: Path, *, readme: str = "# Tool\nReads stdin and writes JSON output.\n") -> None:
    root.mkdir()
    (root / "main.go").write_text("package main\nfunc main() {}\n")
    (root / "go.mod").write_text("module example.com/tool\n")
    (root / "README.md").write_text(readme)


def test_size_bounds_are_warnings_not_blind_rejections(tmp_path):
    root = tmp_path / "tool"
    _go_cli(root)
    draft = screen_source(_manifest(root, loc=1_200, mode="draft"))
    full = screen_source(_manifest(root, loc=1_200, mode="full"))
    assert draft.eligible is True and draft.warnings == []
    assert full.eligible is True
    assert any("below" in warning and "5,000" in warning for warning in full.warnings)


def test_screen_warns_source_without_detected_entrypoint(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "lib.go").write_text("package library\nfunc Parse() {}\n")
    result = screen_source(_manifest(root))
    assert result.eligible is True
    assert "no executable main entrypoint found" in result.warnings


def test_screen_warns_strong_tui_signal_instead_of_rejecting_mixed_tools(tmp_path):
    root = tmp_path / "tui"
    _go_cli(root, readme="# Tool\nA full-screen terminal user interface.\n")
    (root / "go.mod").write_text(
        "module example.com/tool\nrequire github.com/charmbracelet/bubbletea v1.0.0\n"
    )
    result = screen_source(_manifest(root))
    assert result.eligible is True
    assert any("TUI-only" in warning for warning in result.warnings)


def test_screen_warns_remote_only_but_keeps_documented_local_surface_clear(tmp_path):
    remote = tmp_path / "remote"
    _go_cli(remote, readme="# Cloud CLI\nREST API client. Requires authentication and an API token.\n")
    warned = screen_source(_manifest(remote))
    assert warned.eligible is True
    assert any("remote-service client" in warning for warning in warned.warnings)

    local = tmp_path / "local"
    _go_cli(
        local,
        readme="# Cloud CLI\nREST API client, but offline batch mode reads stdin and emits JSON output.\n",
    )
    assert screen_source(_manifest(local)).eligible is True


def test_entrypoint_detection_records_specific_binary_source(tmp_path):
    root = tmp_path / "rust"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.rs").write_text("fn main() {}\n")
    assert detect_cli_entrypoint(root, "Rust") == (True, "src/main.rs")


def test_entrypoint_detection_ignores_tests_examples_and_demos(tmp_path):
    root = tmp_path / "library"
    for rel in ("tests/test_config.c", "examples/main.c", "demo/tool.c"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("int main(void) { return 0; }\n")
    assert detect_cli_entrypoint(root, "C") == (False, "no executable main entrypoint found")


def test_rust_entrypoint_detection_ignores_fuzz_and_testdata_bins(tmp_path):
    root = tmp_path / "rust-library"
    for rel in ("fuzz/Cargo.toml", "tests/testdata/Cargo.toml"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[[bin]]\nname = "fixture"\npath = "main.rs"\n')
    assert detect_cli_entrypoint(root, "Rust") == (
        False,
        "no executable main entrypoint found",
    )


def test_json_output_does_not_erase_remote_api_warning(tmp_path):
    root = tmp_path / "remote-json"
    _go_cli(
        root,
        readme="# Cloud CLI\nREST API client requiring an access token. Supports --json output.\n",
    )
    result = screen_source(_manifest(root))
    assert result.eligible is True
    assert any("remote-service client" in warning for warning in result.warnings)


def test_network_bound_api_stays_warned_even_when_urls_arrive_on_stdin(tmp_path):
    root = tmp_path / "fetcher"
    _go_cli(
        root,
        readme="# Fetcher\nReads URLs from stdin and fetches web page metadata via OpenAI's API.\n",
    )
    result = screen_source(_manifest(root))
    assert result.eligible is True
    assert any("remote-service client" in warning for warning in result.warnings)


def test_reference_to_another_tui_does_not_reject_a_normal_cli(tmp_path):
    root = tmp_path / "converter"
    _go_cli(
        root,
        readme="# Converter\nConverts JSON to structs. Inspired by fx, an interactive terminal tool.\n",
    )
    assert screen_source(_manifest(root)).eligible is True


def test_dossier_contains_bounded_path_labelled_product_evidence(tmp_path):
    root = tmp_path / "converter"
    _go_cli(root, readme="# Converter\nReads CSV from stdin, filters rows, and emits JSON.\n")
    dossier = build_source_dossier(_manifest(root))
    assert dossier["entrypoint_detected"] is True
    paths = {item["path"] for item in dossier["evidence"]}
    assert {"README.md", "main.go", "go.mod"} <= paths
    assert any("filters rows" in item["content"] for item in dossier["evidence"])


def test_discovery_review_requires_entrypoint_and_documented_offline_surface(tmp_path):
    strong = tmp_path / "strong"
    _go_cli(strong, readme="# Converter\nReads CSV from stdin, filters rows, and emits JSON.\n")
    recommendation = recommend_source_review(_manifest(strong))
    assert recommendation.decision == "review" and recommendation.score >= 5
    assert "csv" in recommendation.positive_signals and "stdin" in recommendation.positive_signals

    generic = tmp_path / "generic"
    _go_cli(generic, readme="# Tool\nA delightful command line experience.\n")
    assert recommend_source_review(_manifest(generic)).decision == "low_confidence"
