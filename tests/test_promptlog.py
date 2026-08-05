"""The cell prompt-persistence used by the UI step inspector (promptlog.write_prompt)."""

from programsmith.promptlog import prompt_path, write_prompt


def test_write_prompt_persists_under_run_prompts(tmp_path):
    write_prompt(tmp_path, "TASK_MATRIX", "the exact prompt")
    p = prompt_path(tmp_path, "TASK_MATRIX")
    assert p == tmp_path / "prompts" / "TASK_MATRIX.md"
    assert p.read_text() == "the exact prompt\n"  # newline-terminated for clean file view


def test_write_prompt_never_raises(tmp_path):
    # a persistence failure must not break a run — writing under a path that can't be created is a no-op
    bad = tmp_path / "afile"
    bad.write_text("x")  # now `bad/prompts/...` can't be made (parent is a file)
    write_prompt(bad, "SYNTHESIZE", "p")  # must not raise
