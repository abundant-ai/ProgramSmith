"""Offline tests for the vendored ProgramBench guard: hit (official upstream), miss, the
near-miss lookalike warning (astaxie/bat vs sharkdp/bat class), and URL/ssh parse forms."""

from programsmith.programbench.guard import (
    PROGRAMBENCH_REPOS,
    check_not_programbench,
    is_programbench_upstream,
    parse_github_repo,
)


def test_repo_list_loaded():
    # the vendored data file is the 204-repo official list — a load failure would fail import,
    # but assert the known anchors are present so a truncated copy is caught loudly
    assert ("sharkdp", "bat") in PROGRAMBENCH_REPOS
    assert ("astaxie", "bat") in PROGRAMBENCH_REPOS
    assert len(PROGRAMBENCH_REPOS) > 150


def test_hit_owner_repo_and_url_and_ssh_forms():
    for ref in ("sharkdp/bat", "https://github.com/sharkdp/bat",
                "git@github.com:sharkdp/bat.git", "https://github.com/sharkdp/bat/"):
        ok, reason = check_not_programbench(ref)
        assert not ok, ref
        assert "official ProgramBench" in reason and "sharkdp/bat" in reason
        assert is_programbench_upstream(ref)


def test_miss_plain():
    ok, reason = check_not_programbench("example/definitely-not-a-benchmark-tool")
    assert ok and "not a ProgramBench upstream" in reason
    assert not is_programbench_upstream("example/definitely-not-a-benchmark-tool")


def test_near_miss_same_name_different_owner_warns_but_allows():
    # both astaxie/bat AND sharkdp/bat are official; a third 'bat' by another owner is ALLOWED
    # but must carry the lookalike warning (HANDOFF.md §4 near-miss rule)
    ok, reason = check_not_programbench("someoneelse/bat")
    assert ok
    assert "near-miss" in reason
    assert "sharkdp/bat" in reason and "astaxie/bat" in reason


def test_unparseable_ref_fails_open_with_skip_note():
    # a non-GitHub ref cannot be in the (GitHub-only) official list — guard skips, never blocks
    ok, reason = check_not_programbench("just some local dir name")
    assert ok and "guard skipped" in reason


def test_parse_github_repo_forms():
    assert parse_github_repo("Owner/Repo.git") == ("owner", "repo")
    assert parse_github_repo("https://www.github.com/OWNER/repo") == ("owner", "repo")
    assert parse_github_repo("git@github.com:o/r.git") == ("o", "r")


def test_context_is_carried_in_reason():
    ok, reason = check_not_programbench("sharkdp/bat", context="ingest sharkdp/bat")
    assert not ok and "(ingest sharkdp/bat)" in reason
