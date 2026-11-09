"""Hashing drives idempotency, so what it ignores matters as much as what it sees."""

from kb.ingest.hashing import content_hash, normalise


def test_line_endings_do_not_change_the_hash():
    assert content_hash("a\r\nb") == content_hash("a\nb")


def test_trailing_whitespace_does_not_change_the_hash():
    assert content_hash("a   \nb\t\n") == content_hash("a\nb")


def test_surrounding_blank_lines_do_not_change_the_hash():
    assert content_hash("\n\na\n\n") == content_hash("a")


def test_internal_blank_lines_do_change_the_hash():
    """Conservative on purpose: a hash that ignores a real edit is worse than
    one that fires too often, because the first silently serves stale answers."""
    assert content_hash("a\n\nb") != content_hash("a\nb")


def test_indentation_changes_the_hash():
    assert content_hash("def f():\n    pass") != content_hash("def f():\n  pass")


def test_normalise_is_idempotent():
    once = normalise("a  \r\n\n b \n")
    assert normalise(once) == once
