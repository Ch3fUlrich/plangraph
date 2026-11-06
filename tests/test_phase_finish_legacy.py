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
                                      branch, "0" * 40, repo_root=root, github=github,
                                      pre_patch_phase="demo:PH-X" if legacy else None)
        assert not github.calls
    else:
        result = phase_finish.finish_phase(w.store, w.p("router"), "demo:PH-X",
                                           branch, head, repo_root=root, github=github,
                                           pre_patch_phase="demo:PH-X" if legacy else None)
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
                                  branch, head, repo_root=root, github=github,
                                  pre_patch_phase="demo:PH-X")
    assert not github.calls


@pytest.mark.parametrize("provenance", [None, "demo-PH-X"])
def test_checked_out_legacy_requires_original_key_provenance(w, tmp_path, provenance):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    branch = "phase/demo-PH-X"
    # Both original keys sanitise to this checked-out branch; provenance for
    # one must not authorise the other, even with the exact same valid head.
    assert phase_finish._legacy_phase_branch("demo-PH-X") == branch
    assert phase_finish._legacy_phase_branch("demo:PH-X") == branch
    subprocess.run(["git", "branch", "-M", branch], cwd=root, check=True)
    github = FakeGitHub(ci=True)
    with pytest.raises(Refused, match="branch must be"):
        phase_finish.finish_phase(w.store, w.p("router"), "demo:PH-X",
                                  branch, head, repo_root=root, github=github,
                                  pre_patch_phase=provenance)
    assert not github.calls
