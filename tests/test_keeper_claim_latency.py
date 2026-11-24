"""LE-CLAIM-LATENCY-NEAR-CLI-TIMEOUT: a claim refreshes one repository, shares it, and the CLI waits for it."""
import os

import pytest

from keeper_util import defn
from plangraph.keeper.cli_verbs import Session
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
    # One origin refresh for the item's repository; the other repository is read from its cache.
    assert [call for call in calls if call[1]] == [("demo", True)]


def test_sequential_claims_share_one_origin_refresh_inside_the_window(w, tmp_path, monkeypatch):
    keys = _claim_lanes(w, tmp_path, 3)
    builds = _count_index_builds(monkeypatch)
    for key in keys:
        assert w.store.claim(w.p("l1main"), key)["epoch"] == 1
    assert builds["demo"] == 1


def test_claims_refresh_again_once_the_window_is_zero(w, tmp_path, monkeypatch):
    keys = _claim_lanes(w, tmp_path, 2)
    w.store.claim_index_window_s = 0
    builds = _count_index_builds(monkeypatch)
    for key in keys:
        w.store.claim(w.p("l1main"), key)
    assert builds["demo"] == 2


def test_claim_does_not_refuse_for_another_repository_inventory_gap(w, tmp_path, monkeypatch):
    from plangraph.keeper import context_sources

    keys = _claim_lanes(w, tmp_path, 1)
    original = context_sources.work_index

    def unverified_related(git, project, **kwargs):
        index = original(git, project, **kwargs)
        return dict(index, prs_unverified=True) if project == "related" else index

    monkeypatch.setattr(context_sources, "work_index", unverified_related)
    assert w.store.claim(w.p("l1main"), keys[0])["epoch"] == 1


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


@pytest.mark.parametrize("verb", ["claim", "claim_batch"])
def test_claim_cli_calls_use_the_derived_timeout(srv, w, monkeypatch, verb):
    from argparse import Namespace
    from plangraph.keeper import cli_verbs

    w.store.claim_preflight_s = 41
    seen = []
    session = Session(url=srv.base_url)
    session._token = w.tok("l1main")
    real_call = session.call
    monkeypatch.setattr(session, "call", lambda *a, **k: (seen.append(k.get("timeout")),
                                                          real_call(*a, **k))[1])
    args = Namespace(key="demo:A", keys=["demo:A"], lease=None, force_stale=None)
    getattr(cli_verbs, f"cmd_{verb}")(args, session)
    assert seen == [41 + cli_verbs.CLAIM_COMMIT_MARGIN_S]
