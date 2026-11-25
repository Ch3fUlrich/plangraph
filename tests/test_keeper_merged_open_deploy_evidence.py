"""LE-DEPLOY-EVIDENCE-ON-MERGED-OPEN-ITEM-CIRCULAR: deploy proof on a merged item still open.

The claim of such an item is refused as stale, so the claimant door of
``add_evidence`` accepts kind ``deploy`` on an open item whose Plan-Item commit is on
the default branch, from a principal that could claim it, and audits the commit.
"""
import subprocess

import pytest

from plangraph.keeper import auth, timing
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.events import read_events
from plangraph.keeper.evidence import VERIFIERS
from door_util import Doors
from keeper_util import seed
from test_keeper_run_tokens import FakeRuns
from test_keeper_deploy_gate_paths import _land_with_unit
from test_task_context_guard import _commit, _decide, _git, decisions, repo, w  # noqa: F401 - fixtures
from test_keeper_merge_close_auto import auto, env, git  # noqa: F401 - env is a fixture

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
    env.clock.t = int(git(env.repo.root, "show", "-s", "--format=%ct", "HEAD")) + 1
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


def _trailer_sha(env):
    return git(env.repo.root, "log", "-1", "--format=%H", "--grep", f"Plan-Item: {KEY}", "feat")


@pytest.fixture
def doors(env, tmp_path):  # noqa: F811
    d = Doors(env, tmp_path)
    yield d
    d.close()


def test_merged_open_deploy_evidence_over_http(env, doors):  # noqa: F811
    """Scoped l1 is accepted with the trailer's full SHA in one event; a bound run token is not."""
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    env.store.ctx = {**env.store.ctx, "runs": FakeRuns()}
    _land_with_unit(env.repo.root)
    body = {"key": KEY, "epoch": _epoch(env), "kind": "deploy", "ref": PROOF}
    # A live writer token of another claimed item in the project is bound to that key.
    other_epoch = env.store.claim(env.p("l1main"), "demo:B")["epoch"]
    env.tokens["runw"] = env.store.mint_run_token(env.p("l1main"), "demo:B", "writer", "other")["token"]
    status, err = doors.http("runw", "POST", "/v1/add_evidence", body)
    assert status == 403 and err["error"] == "forbidden" and _events(env) == []
    assert "bound to another item" in err["msg"]
    assert env.conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE key=?", (KEY,)).fetchone()["n"] == 0
    assert other_epoch and env.store._item("demo:B")["state"] == "claimed"
    status, out = doors.http("l1other", "POST", "/v1/add_evidence", body)
    assert status == 403 and _events(env) == []
    status, out = doors.http("l1main", "POST", "/v1/add_evidence", body)
    assert status == 200 and out["verified"] is True
    (event,) = _events(env)
    assert event["payload"]["merged_open"] == _trailer_sha(env) and len(event["payload"]["merged_open"]) == 40


def test_merged_open_deploy_lookup_releases_request_lock(env, doors, monkeypatch):  # noqa: F811
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    seen = {}
    real_project, real_lookup = env.store._require_project, env.store._work_indexes

    def project(*args, **kwargs):
        seen["held_before"] = timing._request_timer.get() is not None
        return real_project(*args, **kwargs)

    def lookup(*args, **kwargs):
        seen["held_in_lookup"] = timing._request_timer.get() is not None
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(env.store, "_require_project", project)
    monkeypatch.setattr(env.store, "_work_indexes", lookup)
    status, _ = doors.http("l1main", "POST", "/v1/add_evidence",
                           {"key": KEY, "epoch": _epoch(env), "kind": "deploy", "ref": PROOF})
    assert status == 200
    assert seen == {"held_before": True, "held_in_lookup": False}


def test_merged_open_deploy_refuses_changed_epoch(env, monkeypatch):  # noqa: F811
    """A claim and release during the lookup moves the epoch: nothing is written."""
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    epoch = _epoch(env)
    real, fired = env.store._work_indexes, []

    def lookup_then_cycle(*args, **kwargs):
        out = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            env.store.claim(env.p("l1main"), KEY, force_stale="test: move the epoch")
            env.store.release(env.p("l1main"), KEY, _epoch(env))
        return out

    monkeypatch.setattr(env.store, "_work_indexes", lookup_then_cycle)
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p("l1main"), KEY, epoch, "deploy", PROOF)
    assert exc.value.code == "epoch_stale" and fired
    assert _epoch(env) != epoch
    assert _events(env) == []
    assert env.conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE key=?", (KEY,)).fetchone()["n"] == 0


def test_merged_open_deploy_refuses_merge_body_only(env):  # noqa: F811
    """A merge message that mentions the Plan-Item outside its trailer block is not a trailer."""
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    root = env.repo.root
    git(root, "checkout", "-q", "-b", "feat", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "work without a trailer")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m",
        f"Merge pull request #7 from x/feat\n\nPlan-Item: {KEY}\n\nProse after the line ends the trailer block.", "feat")
    with pytest.raises(Refused, match="already merged"):
        env.store.claim(env.p("l1main"), KEY)
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p("l1main"), KEY, _epoch(env), "deploy", PROOF)
    assert exc.value.code == "bad_state" and "no Plan-Item commit" in exc.value.msg
    assert _events(env) == []
    assert env.conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE key=?", (KEY,)).fetchone()["n"] == 0


def test_merged_open_deploy_refuses_a_definition_edited_during_verification(env, monkeypatch):  # noqa: F811
    """A Merge-Repo edit that keeps the epoch must not keep a verdict from the old text."""
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    _land_with_unit(env.repo.root)
    epoch = _epoch(env)
    real, fired = env.store._verify_evidence, []

    def verify_then_edit(*args, **kwargs):
        out = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            env.store.edit_definition(env.p("router"), KEY, {"note": "Merge-Repo: other"})
        return out

    monkeypatch.setattr(env.store, "_verify_evidence", verify_then_edit)
    with pytest.raises(Refused) as exc:
        env.store.add_evidence(env.p("l1main"), KEY, epoch, "deploy", PROOF)
    assert fired and _epoch(env) == epoch
    assert exc.value.code == "conflict" and "edited" in exc.value.msg
    assert _events(env) == []
    assert env.conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE key=?", (KEY,)).fetchone()["n"] == 0
