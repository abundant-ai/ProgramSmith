"""The orphan-cell reaper match must be narrow: only orphaned (PPID 1) headless `claude -p` cells
carrying our FOCUS_PROMPT — never the host Claude session, a live cell, or an unrelated process."""

from programsmith.procreap import orphan_cell_pids

_SIG = "headless worker executing ONE narrowly-scoped task"


def test_reaps_only_orphaned_our_cells():
    lines = [
        # orphaned (ppid 1) OUR cell → REAP
        f"35154 1 claude -p --model claude-sonnet-4-6 --add-dir x {_SIG} blah",
        f"4905 1 claude -p --output-format json {_SIG}",
        f"8123 1 claude --bare -p --model claude-opus-4-8 {_SIG}",
        # live cell under the current server (ppid != 1) → KEEP
        f"65968 65826 claude -p --model claude-sonnet-4-6 {_SIG}",
        # the HOST Claude Code session (not `claude -p`, no signature) → KEEP
        "37679 1 /Applications/.../claude --output-format stream-json --verbose --model claude-opus-4-8",
        # an unrelated orphaned claude -p WITHOUT our signature → KEEP (not ours)
        "999 1 claude -p --model sonnet do something unrelated",
        # junk / header lines
        "PID PPID COMMAND",
        "",
    ]
    assert orphan_cell_pids(lines) == [35154, 4905, 8123]


def test_no_orphans_on_clean_start():
    lines = [f"65968 65826 claude -p {_SIG}", "37679 1 /x/claude --output-format stream-json"]
    assert orphan_cell_pids(lines) == []
