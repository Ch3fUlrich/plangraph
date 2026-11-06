"""Cross-language branch vectors, mirrored in the Rust phase helper tests."""
import pytest

from plangraph.planner.phase_finish import phase_branch
from plangraph.planner.phase_finish import finish_phase, _legacy_phase_branch
from plangraph.keeper.errors import Refused
from types import SimpleNamespace
from phase_finish_support import git, git_repo


@pytest.mark.parametrize(("key", "expected"), [
    ("a/b", "phase/h/e620c3190468cf61"),
    ("a-b", "phase/h/e61ab31904644883"),
    ("a:b", "phase/h/e661911904a01160"),
    ("é", "phase/h/0ac21707b7181e01"),
    ("😀", "phase/h/feff073875020288"),
    ("e\u0301", "phase/h/c0c9d418ee802e1f"),
])
def test_original_utf8_hash_vectors(key, expected):
    assert phase_branch(key) == expected
    assert phase_branch(key) == phase_branch(key)


def test_sanitiser_collisions_and_literal_suffix_are_distinct():
    keys = ["a/b", "a-b", "a:b", "a b", "a-b-e620c3190468cf61"]
    assert len({phase_branch(key) for key in keys}) == len(keys)
    for key in keys:
        assert phase_branch(key).startswith("phase/h/")
        assert "/" not in _legacy_phase_branch(key).removeprefix("phase/")
    assert phase_branch("a/b") != _legacy_phase_branch("a-b-e620c3190468cf61")


@pytest.mark.parametrize("key", ["a/b", "a-b", "é", "😀", "e\u0301"])
def test_finish_rejects_legacy_branch_when_repo_is_missing_before_store_read(key):
    class UnreadableStore:
        def __getattr__(self, name):
            raise AssertionError("legacy branch reached store")

    with pytest.raises(Refused, match="branch must be"):
        finish_phase(UnreadableStore(), SimpleNamespace(role="router"), key,
                     _legacy_phase_branch(key), "a" * 40,
                     repo_root="unused", github=None)


def test_finish_rejects_other_legacy_checkout_before_store_read(tmp_path):
    root, head = git_repo(tmp_path)
    git(root, "branch", "-M", "phase/other")

    class UnreadableStore:
        def __getattr__(self, name):
            raise AssertionError("wrong legacy checkout reached store")

    with pytest.raises(Refused, match="branch must be"):
        finish_phase(UnreadableStore(), SimpleNamespace(role="router"), "a/b",
                     _legacy_phase_branch("a/b"), head, repo_root=root, github=None)
