"""Hosted PR provenance, independent of forgeable git topology."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from evidence_fakes import FakeCI, ITEM, SHA, MERGE_SHA, make_ctx, verify
from git_helpers import git_cli
from plangraph.keeper.evidence_ci import CIAdapter
from plangraph.keeper.evidence_git import GitAdapter


def pr(sha=MERGE_SHA, repository="owner/repo", branch="main", merged="now"):
    return {"merged_at": merged, "merge_commit_sha": sha,
            "base": {"repo": {"full_name": repository}, "ref": branch}}


def page_stdout(pages):
    return ("\n".join(json.dumps(page) for page in pages)
            if isinstance(pages, list) else json.dumps(pages))


@pytest.fixture
def topology(tmp_path, monkeypatch):
    git, init = git_cli(tmp_path, monkeypatch)
    init(tmp_path, "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Merger")
    git(tmp_path, "config", "user.email", "merger@example.invalid")
    def commit(message):
        git(tmp_path, "commit", "--allow-empty", "-m", message,
            "-m", "Plan-Item: " + ITEM["key"])
        return git(tmp_path, "rev-parse", "HEAD")
    commit("base")
    git(tmp_path, "switch", "-c", "lane")
    commit("work")
    git(tmp_path, "switch", "main")
    commit("main work")
    git(tmp_path, "switch", "lane")
    git(tmp_path, "merge", "--no-ff", "-m", "lane head\n\nPlan-Item: " + ITEM["key"], "main")
    lane = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "--no-ff", "-m", "outer merge\n\nPlan-Item: " + ITEM["key"], "lane")
    outer = git(tmp_path, "rev-parse", "HEAD")
    adapter = GitAdapter()
    adapter.repo_path = lambda project: str(tmp_path)
    adapter.repo_ref = lambda project: "main"
    return adapter, lane, outer


def test_direct_push_refused_with_allowed_committer_and_green_ci(topology):
    git, lane, outer = topology
    ci = FakeCI(runs=[{"name": "build", "head_sha": outer, "conclusion": "success"}])
    ci.merged_pr_for = lambda *args: None
    ctx = make_ctx(git=git, ci=ci, merge_principals=("merger@example.invalid",))
    assert git.merge_commit_of(git.repo_path("routing"), outer, "main")[0] == outer
    result = verify("commit+ci", {"commit": outer}, ctx)
    assert not result.ok and "merged_pr_unproven" in result.detail


@pytest.mark.parametrize("use_lane", [False, True])
def test_matching_outer_pr_passes(topology, use_lane):
    git, lane, outer = topology
    sha = lane if use_lane else outer
    calls = []
    ci = FakeCI(runs=[{"name": "build", "head_sha": sha, "conclusion": "success"}])
    def lookup(project, merge, target):
        calls.append((project, merge, target))
        return pr(outer)
    ci.merged_pr_for = lookup
    result = verify("commit+ci", {"commit": sha}, make_ctx(git=git, ci=ci))
    assert result.ok, result.detail
    assert calls == [("routing", outer, "main")]


@pytest.mark.parametrize("pages", [
    [[pr(sha=SHA)]], [[pr(repository="other/repo")]],
    [[pr(branch="develop")]], [[pr(merged=None)]], [[]], [],
    {}, [None], [[None]], [[pr()], {}], [[pr()], [42]],
])
def test_unproven_pages_refuse(monkeypatch, pages):
    adapter = CIAdapter({"routing": "owner/repo"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, page_stdout(pages), ""))
    result = verify("commit+ci", {"commit": SHA}, make_ctx(ci=adapter))
    assert not result.ok and "merged_pr_unproven" in result.detail


@pytest.mark.parametrize("pages", [[[]], {}, [None], [[None]], [[pr()], {}]])
def test_empty_pages_return_none_but_malformed_pages_raise(monkeypatch, pages):
    adapter = CIAdapter({"routing": "owner/repo"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, page_stdout(pages), ""))
    if pages == [[]]:
        assert adapter.merged_pr_for("routing", MERGE_SHA, "main") is None
    else:
        with pytest.raises(ValueError, match="malformed PR pages"):
            adapter.merged_pr_for("routing", MERGE_SHA, "main")


@pytest.mark.parametrize("failure", ["json", "truncated", "error", "timeout"])
def test_command_failures_refuse(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("gh", 1)
        return subprocess.CompletedProcess(args, 1 if failure == "error" else 0,
                                           "[[" if failure == "truncated" else "not-json", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = verify("commit+ci", {"commit": SHA},
                    make_ctx(ci=CIAdapter({"routing": "owner/repo"})))
    assert not result.ok and "merged_pr_unproven" in result.detail


@pytest.mark.parametrize("mode", [None, "none", "forgejo:owner/repo", "bad?slug/repo",
                                  "../repo", "owner/repo/extra"])
def test_unsupported_identity_refuses_without_command(monkeypatch, mode):
    def unexpected(*a, **kw):
        pytest.fail("unsupported identity reached subprocess")
    monkeypatch.setattr(subprocess, "run", unexpected)
    adapter = CIAdapter({} if mode is None else {"routing": mode})
    result = verify("commit+ci", {"commit": SHA}, make_ctx(ci=adapter))
    assert not result.ok and "merged_pr_unproven" in result.detail


def test_missing_adapter_method_refuses():
    for ci in (None, SimpleNamespace(mode=lambda project: "none")):
        result = verify("commit+ci", {"commit": SHA}, make_ctx(ci=ci))
        assert not result.ok and "merged_pr_unproven" in result.detail


@pytest.mark.parametrize("separator", ["\n", ""])
def test_pagination_exact_match_safe_command_and_timeout(monkeypatch, separator):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, separator.join(map(json.dumps, [
            [pr(sha=SHA), pr(merged=None)], [pr()]])), "")
    monkeypatch.setattr(subprocess, "run", run)
    adapter = CIAdapter({"routing": "owner/repo"}, timeout=7)
    assert adapter.merged_pr_for("routing", MERGE_SHA, "origin/main") == pr()
    assert calls == [(["gh", "api", "--paginate",
                      f"repos/owner/repo/commits/{MERGE_SHA}/pulls"],
                     {"capture_output": True, "text": True, "timeout": 7, "check": False})]
    with pytest.raises(ValueError):
        adapter.merged_pr_for("routing", "--unsafe", "main")
    assert len(calls) == 1


def test_checkout_origin_identity_and_remote_branch(topology, monkeypatch):
    git, lane, outer = topology
    repo = git.repo_path("routing")
    subprocess.run(["git", "-C", repo, "remote", "add", "upstream",
                    "https://github.com/owner/repo.git"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin",
                    "https://github.com/owner/repo.git"], check=True)
    real_run = subprocess.run
    def run(args, **kwargs):
        if args[0] == "gh":
            return subprocess.CompletedProcess(args, 0, json.dumps([pr(outer)]), "")
        return real_run(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    adapter = CIAdapter({"routing": repo})
    assert adapter.merged_pr_for("routing", outer, "upstream/main") == pr(outer)


@pytest.mark.parametrize("case,detail", [
    ("floor", "floor"), ("trailer", "Plan-Item"),
    ("principal", "merge principal"), ("ci", "not green"),
])
def test_existing_refusals_preserved(case, detail):
    ctx = make_ctx()
    if case == "floor":
        ctx.bind_floor_required = True
        ctx.bind_floor_sha = "e" * 40
        ctx.git.descends = False
    elif case == "trailer":
        ctx.git.message_trailers = {}
    elif case == "principal":
        ctx.merge_principals = ("other@example.invalid",)
    else:
        ctx.ci.runs[0]["conclusion"] = "failure"
    result = verify("commit+ci", {"commit": SHA}, ctx)
    assert not result.ok and ("does not descend" if case == "floor" else detail) in result.detail


def test_routing_data_commit_evidence_d3999(topology):
    git, lane, outer = topology
    item = {"project": "plangraph-data", "track": "ROUTING", "id": "D-3999"}
    # Router-only commit evidence makes no PR claim and requires no CI adapter.
    result = verify("commit", {"commit": outer}, make_ctx(git=git, ci=None), item)
    assert result.ok, result.detail


@pytest.mark.parametrize("stdout", [
    "", " \n\t", json.dumps([pr()]) + "garbage",
    json.dumps({}), json.dumps([pr()]) + "\n{}",
    json.dumps([pr()]) + "[42]", json.dumps([[pr()]]),
    "null", "42",
])
def test_malformed_page_stream_raises(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, stdout, ""))
    adapter = CIAdapter({"routing": "owner/repo"})
    with pytest.raises(ValueError, match="^malformed PR pages$"):
        adapter.merged_pr_for("routing", MERGE_SHA, "main")


@pytest.mark.parametrize("case,expected", [
    ("marked", True), ("unmarked", False), ("red", False),
    ("off_target", False), ("direct", False), ("no_ci", False),
    ("string_flag", False),
    ("wrong_ref", False), ("missing_ref", False), ("invalid_ref", False),
])
def test_non_pr_merge_proof(topology, case, expected):
    git, lane, outer = topology
    repo = git.repo_path("routing")
    git._repos = {"routing": {"path": repo, "ref": "main",
                              "non_pr": "true" if case == "string_flag"
                               else case != "unmarked"}}
    if case == "wrong_ref":
        git.repo_ref = lambda project: "lane"
    elif case == "missing_ref":
        del git._repos["routing"]["ref"]
    elif case == "invalid_ref":
        git._repos["routing"]["ref"] = "--unsafe"
    sha = outer
    if case == "wrong_ref":
        sha = lane
    if case == "off_target":
        subprocess.run(["git", "-C", repo, "update-ref", "refs/heads/main",
                        outer + "^"], check=True)
    elif case == "direct":
        subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m",
                        "direct\n\nPlan-Item: " + ITEM["key"]], check=True)
        sha = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"],
                                      text=True).strip()
    ci = FakeCI(runs=[{"name": "build", "head_sha": sha,
                       "conclusion": "failure" if case == "red" else "success"}])
    ci.merged_pr_for = lambda *args: None
    if case == "no_ci":
        ci.mode = lambda project: "none"
    result = verify("commit+ci", {"commit": sha}, make_ctx(git=git, ci=ci))
    assert result.ok is expected, result.detail


@pytest.mark.parametrize("case,detail", [
    ("trailer", "Plan-Item"), ("floor", "does not descend"),
    ("principal", "merge principal"), ("ci", "not green"),
])
def test_non_pr_preserves_verifier_guards(topology, monkeypatch, case, detail):
    git, lane, outer = topology
    repo = git.repo_path("routing")
    git._repos = {"routing": {"path": repo, "ref": "main", "non_pr": True}}
    ci = FakeCI(runs=[{"name": "build", "head_sha": outer,
                       "conclusion": "failure" if case == "ci" else "success"}])
    ci.merged_pr_for = lambda *args: pytest.fail("non-PR proof must suffice")
    ctx = make_ctx(git=git, ci=ci)
    if case == "trailer":
        monkeypatch.setattr(git, "trailers", lambda *args: {})
    elif case == "floor":
        ctx.bind_floor_required = True
        ctx.bind_floor_sha = lane
        ancestor = git.is_ancestor
        monkeypatch.setattr(git, "is_ancestor", lambda repo, sha, ref:
                            False if ref == outer else ancestor(repo, sha, ref))
    elif case == "principal":
        ctx.merge_principals = ("disallowed",)
    result = verify("commit+ci", {"commit": outer}, ctx)
    assert not result.ok and detail in result.detail
