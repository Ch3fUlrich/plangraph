"""The Sonnet review leg gets its bound diff on every spawn path (scoped Bash cannot run git diff)."""
import asyncio
import inspect
import subprocess
import time

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS, REVIEW_SONNET
from plangraph.spawner.inline_diff import InlineDiffError, review_sonnet_diff_prompt


def _repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=f", "-c", "user.email=f@e.test",
                              *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "a.txt").write_text("two\n")
    git("commit", "-qam", "head")
    return {"repo": str(tmp_path), "base": base, "head": git("rev-parse", "HEAD")}


def _leg(leg_id):
    return next(leg for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS) if leg.id == leg_id)


def test_sonnet_review_gets_the_inline_diff(tmp_path):
    card = {"diff_ref": _repo(tmp_path)}
    out = asyncio.run(review_sonnet_diff_prompt("review", card, "review", _leg(REVIEW_SONNET)))
    assert "+two" in out and "use repository tools to verify" in out


@pytest.mark.parametrize("role, leg_id, with_ref", [
    ("review", "opencode-go-deepseek-v4-flash", True),
    ("implement", REVIEW_SONNET, True),
    ("review", REVIEW_SONNET, False),
])
def test_other_legs_roles_and_cards_are_unchanged(tmp_path, role, leg_id, with_ref):
    card = {"diff_ref": _repo(tmp_path)} if with_ref else {}
    assert asyncio.run(review_sonnet_diff_prompt("review", card, role, _leg(leg_id))) == "review"


def test_expired_deadline_refuses_before_running_git(tmp_path):
    card = {"diff_ref": _repo(tmp_path)}
    with pytest.raises(InlineDiffError, match="deadline"):
        asyncio.run(review_sonnet_diff_prompt("review", card, "review", _leg(REVIEW_SONNET),
                                              deadline=time.time() - 1))


def test_every_spawn_path_packages_the_sonnet_diff():
    # DispatchService._spawn (runner.py) once skipped the step the service path had.
    from plangraph.spawner import runner, service
    for module in (runner, service):
        source = inspect.getsource(module)
        assert source.count("_prepare_leg_prompt(") == source.count("review_sonnet_diff_prompt("), module
        assert "review_sonnet_diff_prompt" in source, module
