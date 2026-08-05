import pytest

from programsmith.runkey import validate_run_key


def test_run_key_accepts_slug_and_rejects_traversal():
    assert validate_run_key("ripgrep-v2.1") == "ripgrep-v2.1"
    for value in ("../outside", "a/b", "", ".hidden", "bad name"):
        with pytest.raises(ValueError):
            validate_run_key(value)
