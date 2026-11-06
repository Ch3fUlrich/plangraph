import subprocess

import pytest

from plangraph.keeper.errors import Refused
from plangraph.planner import phase_finish
from phase_finish_support import FakeGitHub, finish_cards, gate, git_repo


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("wrong_tip", [False, True])
def test_finish_locked_branch_versions(w, tmp_path, legacy, wrong_tip):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    branch = "phase/demo-PH-X" if legacy else phase_finish.phase_branch("demo:PH-X")
    subprocess.run(["git", "branch", "-M", branch], cwd=root, check=True)
    github = FakeGitHub(ci=True)
    if wrong_tip:
        with pytest.raises(Refused, match="local branch tip differs"):
            phase_finish.finish_phase(w.store, w.p("router"), "demo:PH-X",
                                      branch, "0" * 40, repo_root=root, github=github)
        assert not github.calls
    else:
        result = phase_finish.finish_phase(w.store, w.p("router"), "demo:PH-X",
                                           branch, head, repo_root=root, github=github)
        assert result["state"] == "merged"
        assert result["branch"] == branch
        assert github.calls[0] == ("find_pr", branch)


@pytest.mark.parametrize("detached", [False, True])
def test_legacy_ref_without_checkout_is_not_trusted(w, tmp_path, detached):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    branch = "phase/demo-PH-X"
    subprocess.run(["git", "branch", branch, head], cwd=root, check=True)
    if detached:
        subprocess.run(["git", "checkout", "--detach", head], cwd=root, check=True)
    github = FakeGitHub(ci=True)
    with pytest.raises(Refused, match="branch must be"):
        phase_finish.finish_phase(w.store, w.p("router"), "demo:PH-X",
                                  branch, head, repo_root=root, github=github)
    assert not github.calls
