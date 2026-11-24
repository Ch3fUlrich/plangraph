"""LE-CLAIM-LATENCY-NEAR-CLI-TIMEOUT: a claim refreshes one repository, shares it, and the CLI waits for it."""
import os

import pytest

from keeper_util import defn
from plangraph.keeper.cli_verbs import Session
from plangraph.keeper.errors import Refused
from test_keeper_api import _claim_checkouts, call, jbody, srv, w  # noqa: F401  (fixtures and helpers)


def _spy_work_indexes(monkeypatch):
    from plangraph.keeper import context_sources

    calls, original = [], context_sources.work_index

    def spy(git, project, **kwargs):
        calls.append((project, bool(kwargs.get("fresh"))))
        return original(git, project, **kwargs)

    monkeypatch.setattr(context_sources, "work_index", spy)
    return calls


def _count_index_builds(monkeypatch):
    """Real index rebuilds (one origin refresh each) per repository path."""
    from collections import Counter
    from plangraph.keeper import context_sources

    builds, original = Counter(), context_sources._work_log

    def counted(git, repo, *args, **kwargs):
        builds[os.path.basename(str(repo))] += 1
        return original(git, repo, *args, **kwargs)

    monkeypatch.setattr(context_sources, "_work_log", counted)
    return builds


def _claim_lanes(w, tmp_path, count, repos=("demo", "related")):
    w.store.ctx = {"git": _claim_checkouts(tmp_path, repos)}
    ids = [f"FAST-{i}" for i in range(count)]
    w.store.create_items(w.p("router"), [*(defn(key) for key in ids),
                                           defn("FAST-GOAL", ids, kind="goal")])
    return [f"demo:{key}" for key in ids]


def test_claim_batch_runs_one_fresh_preflight_per_repository(w, tmp_path, monkeypatch):
    keys = _claim_lanes(w, tmp_path, 5)
    calls = _spy_work_indexes(monkeypatch)
    rows = w.store.claim_batch(w.p("l1main"), keys)["results"]
    assert [row["epoch"] for row in rows] == [1] * 5
    # Five keys, two repositories (the other one was never built): one origin refresh each.
    assert sorted(call for call in calls if call[1]) == [("demo", True), ("related", True)]


def test_a_recent_other_repository_is_not_refreshed_by_the_next_claim(w, tmp_path, monkeypatch):
    keys = _claim_lanes(w, tmp_path, 2)
    builds = _count_index_builds(monkeypatch)
    for key in keys:
        w.store.claim(w.p("l1main"), key)
    # The item's repository is rebuilt for every claim; the other one, built a moment ago, is served as built.
    assert builds["demo"] == 2
    assert builds["related"] == 1


def test_a_claim_refreshes_another_repository_whose_index_expired(w, tmp_path, monkeypatch):
    from subprocess import run

    keys = _claim_lanes(w, tmp_path, 2)
    w.store.claim(w.p("l1main"), keys[0])
    git = w.store._adapter("git")
    # Age every built index past the ready read's TTL, then merge the second item's work in the other repository.
    git._context_work_index = {k: (stamp - 3600, index) for k, (stamp, index) in git._context_work_index.items()}
    run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-qm",
         f"work\n\nPlan-Item: {keys[1]}"], cwd=git.repo_path("related"), check=True)
    builds = _count_index_builds(monkeypatch)
    with pytest.raises(Refused, match="already merged"):
        w.store.claim(w.p("l1main"), keys[1])
    assert builds["related"] == 1 and w.store._item(keys[1])["state"] == "open"


def test_health_reports_the_claim_budget_and_the_cli_timeout_follows_it(srv, w, monkeypatch):
    from plangraph.keeper import cli_verbs

    w.store.claim_preflight_s = 41
    assert call(srv, "/v1/health")[0] == 200
    assert jbody(call(srv, "/v1/health")[2]) == {"status": "ok", "claim_preflight_s": 41}
    session = Session(url=srv.base_url)
    assert session.claim_timeout() == 41 + cli_verbs.CLAIM_COMMIT_MARGIN_S
    assert cli_verbs.CLAIM_COMMIT_MARGIN_S > 0
    w.store.claim_preflight_s = 7
    assert Session(url=srv.base_url).claim_timeout() == 7 + cli_verbs.CLAIM_COMMIT_MARGIN_S


def test_claim_timeout_without_a_keeper_answer_uses_the_default_budget(monkeypatch):
    from plangraph.keeper import cli_verbs, timing

    session = Session(url="http://127.0.0.1:9")
    assert session.claim_timeout() == (timing.DEFAULT_CLAIM_PREFLIGHT_S
                                       + cli_verbs.CLAIM_COMMIT_MARGIN_S)


@pytest.mark.parametrize(("verb", "body"), [("claim", {"key": "demo:A"}), ("claim_batch", {"keys": ["demo:A"]})])
def test_claim_cli_calls_use_the_derived_timeout(srv, w, monkeypatch, verb, body):
    import urllib.request
    from plangraph.keeper import cli_verbs

    w.store.claim_preflight_s = 41
    seen, urlopen = {}, urllib.request.urlopen

    def spy(request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        seen[url.split("?")[0].rsplit("/", 1)[-1]] = timeout
        return urlopen(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    session = Session(url=srv.base_url)
    session._token = w.tok("l1main")
    session.call("POST", verb, body=body)
    session.call("GET", "show/demo:A")
    assert seen[verb] == 41 + cli_verbs.CLAIM_COMMIT_MARGIN_S
    assert seen["demo:A"] == cli_verbs.TIMEOUT  # other verbs keep the fixed bound
