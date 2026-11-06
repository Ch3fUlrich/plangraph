"""Cross-language branch vectors, mirrored in the Rust phase helper tests."""
import pytest

from plangraph.planner.phase_finish import phase_branch


@pytest.mark.parametrize(("key", "expected"), [
    ("a/b", "phase/a-b-e620c3190468cf61"),
    ("a-b", "phase/a-b-e61ab31904644883"),
    ("a:b", "phase/a-b-e661911904a01160"),
    ("é", "phase/--0ac21707b7181e01"),
    ("😀", "phase/--feff073875020288"),
    ("e\u0301", "phase/e--c0c9d418ee802e1f"),
])
def test_original_utf8_hash_vectors(key, expected):
    assert phase_branch(key) == expected
    assert phase_branch(key) == phase_branch(key)


def test_sanitiser_collisions_and_literal_suffix_are_distinct():
    keys = ["a/b", "a-b", "a:b", "a b", "a-b-e620c3190468cf61"]
    assert len({phase_branch(key) for key in keys}) == len(keys)
