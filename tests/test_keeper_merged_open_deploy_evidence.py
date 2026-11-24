"""LE-DEPLOY-EVIDENCE-ON-MERGED-OPEN-ITEM-CIRCULAR: deploy proof on a merged item still open.

The claim of such an item is refused as stale, so the claimant door of
``add_evidence`` accepts kind ``deploy`` on an open item whose Plan-Item commit is on
the default branch, from a principal that could claim it, and audits the commit.
"""
import subprocess

import pytest

from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.events import read_events
from plangraph.keeper.evidence import VERIFIERS
from keeper_util import seed
from test_keeper_deploy_gate_paths import _land_with_unit
from test_task_context_guard import _commit, _decide, _git, decisions, repo, w  # noqa: F401 - fixtures
from test_keeper_merge_close_auto import auto, env  # noqa: F401 - env is a fixture

KEY = "demo:A"
PROOF = {"host": "fixture", "unit": "new.service", "is_active": "active"}


def _epoch(env, key=KEY):
    return env.store._item(key)["epoch"]


def _events(env, key=KEY):
    return [e for e in read_events(env.conn) if e["type"] == "evidence_added" and e["key"] == key]


def test_a_merged_open_item_takes_deploy_evidence_without_a_claim_and_then_closes(env):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    env.store.edit_definition(env.p("router"), KEY, {"note": "Deploy-Hosts: fixture"})
    _land_with_unit(env.repo.root)
    with pytest.raises(Refused, match="already merged"):
        env.store.claim(env.p("l1main"), KEY)
    assert auto(env, require_seats=False)["closed"] == []
    out = env.store.add_evidence(env.p("l1main"), KEY, _epoch(env), "deploy", PROOF)
    assert out["verified"] is True
    assert env.store._item(KEY)["state"] == "open"
    assert env.store._item(KEY)["claimed_by"] is None
    (event,) = _events(env)
    assert event["payload"]["merged_open"] and event["actor"] == "l1main"
    result = auto(env, require_seats=False)
    assert [c["key"] for c in result["closed"]] == [KEY], result["listed"]


def test_a_worker_scoped_to_the_project_may_attach_it_too(env):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    assert env.store.add_evidence(env.p("workerA"), KEY, _epoch(env), "deploy", PROOF)["verified"]


def test_an_open_item_that_is_not_merged_still_refuses_deploy_evidence(env):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p("l1main"), KEY, _epoch(env), "deploy", PROOF)
    assert exc.value.code == "bad_state" and "no Plan-Item commit" in exc.value.msg
    assert _events(env) == []


@pytest.mark.parametrize("who", ["l1other", "reader", "reviewer"])
def test_a_principal_that_could_not_claim_it_is_refused(env, who):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p(who), KEY, _epoch(env), "deploy", PROOF)
    assert exc.value.code == "forbidden"
    assert _events(env) == []


def test_only_deploy_evidence_is_taken_on_an_open_merged_item(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    with pytest.raises(Refused, match="not claimed"):
        env.store.add_evidence(env.p("l1main"), KEY, _epoch(env), "commit+ci", {"sha": "x"})


def test_a_stale_epoch_is_refused(env):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p("l1main"), KEY, _epoch(env) + 1, "deploy", PROOF)
    assert exc.value.code == "epoch_stale"


def test_a_principal_revoked_during_the_unlocked_lookup_writes_nothing(env, monkeypatch):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    real = env.store._work_indexes

    def lookup_then_revoke(*args, **kwargs):
        out = real(*args, **kwargs)
        auth.revoke(env.conn, "l1main")
        return out

    monkeypatch.setattr(env.store, "_work_indexes", lookup_then_revoke)
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p("l1main"), KEY, _epoch(env), "deploy", PROOF)
    assert exc.value.code == "forbidden"
    assert _events(env) == []


def test_a_run_token_is_refused_on_an_open_item(env):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    item = env.store._item(KEY)
    run = auth.RunPrincipal(name="run", role="worker", projects=["demo"], family=None,
                            token_role="writer", key=KEY, epoch=item["epoch"])
    with pytest.raises(Refused) as exc:
        env.store._merged_open_deploy_evidence(run, item, item["epoch"], PROOF, env.clock.t)
    assert exc.value.code == "forbidden"


def test_an_item_merged_in_another_scanned_repository_takes_the_evidence(env, tmp_path):  # noqa: F811
    """The claim sees a qualified Plan-Item trailer in any scanned repository; so does this door."""
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    other = tmp_path / "other"
    other.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.invalid"),
                 ("config", "user.name", "t"), ("config", "commit.gpgsign", "false"),
                 ("commit", "-q", "--allow-empty", "-m", f"work\n\nPlan-Item: {KEY}")):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    env.store.ctx = {**env.store.ctx, "git": GitAdapter({"demo": str(env.repo.root), "other": str(other)})}
    with pytest.raises(Refused, match="already merged"):
        env.store.claim(env.p("l1main"), KEY)
    out = env.store.add_evidence(env.p("l1main"), KEY, _epoch(env), "deploy", PROOF)
    assert out["verified"] is True and len(_events(env)) == 1


def test_a_decision_only_delivery_is_no_plan_item_commit_and_takes_no_evidence(w, repo, decisions):  # noqa: F811
    """The claim refuses a close-decision delivery as stale, but it names no Plan-Item commit here."""
    key, = seed(w, [("CLOSE-TOKEN", [])])
    _git(repo, "checkout", "-qb", "part")
    _commit(repo, "legacy-work", "work before item trailers")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Land legacy work (#56)", "part")
    merged = _git(repo, "rev-parse", "HEAD")
    _decide(decisions, 56, f"Router checked merged delivery. Closes: {key}@{merged}")
    w.store.verifiers["deploy"] = VERIFIERS["deploy"]
    with pytest.raises(Refused, match="already merged: Close decision D-56"):
        w.store.claim(w.p("l1main"), key)
    epoch = w.store._item(key)["epoch"]
    with pytest.raises(Refused) as exc:
        w.store.add_evidence(w.p("l1main"), key, epoch, "deploy", PROOF)
    assert exc.value.code == "bad_state" and "no Plan-Item commit" in exc.value.msg
    assert [e for e in read_events(w.conn) if e["type"] == "evidence_added"] == []
