"""Merge proof must be bound, repeatable, and all-or-nothing."""
import subprocess
from types import SimpleNamespace

import pytest

from keeper_util import World
from test_phase_finish import gate, finish_cards
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_types import Result
from plangraph.planner.phase_finish import attach_merge_evidence


@pytest.fixture
def setup(tmp_path):
    world = World(tmp_path / "keeper")
    gate(world)
    finish_cards(world, "demo:C1", "demo:C2")
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=t", "-c", "user.email=t@invalid", *args],
            cwd=root, text=True).strip()

    git("init", "-q")
    git("commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "topic")
    git("commit", "--allow-empty", "-qm", "topic")
    head = git("rev-parse", "HEAD")
    git("checkout", "--detach", base)
    git("merge", "--no-ff", "topic", "-m", "merge")
    merge = git("rev-parse", "HEAD")
    pr = {"state": "merged", "head": head, "merge_sha": merge}
    gh = SimpleNamespace(find_pr=lambda branch: pr)
    world.store._verify_evidence = lambda *args: Result(True, "ok", args[2])
    yield world, root, gh, pr, merge, base
    world.close()


def counts(w):
    return tuple(w.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("evidence", "events"))


def attach(setup, **kwargs):
    w, root, gh, pr, merge, base = setup
    return attach_merge_evidence(w.store, w.p("router"), "demo:PH-X", merge,
                                 github=gh, **kwargs)


def test_repeat_is_idempotent(setup):
    first = attach(setup)
    before = counts(setup[0])
    assert before[0] == 2
    assert attach(setup) == first
    assert counts(setup[0]) == before


@pytest.mark.parametrize("wrong", ["merge", "head", "missing"])
def test_binding_refuses_without_writes(setup, wrong):
    w, root, gh, pr, merge, base = setup
    if wrong == "merge":
        pr["merge_sha"] = base
    elif wrong == "head":
        pr["head"] = base
    else:
        del pr["merge_sha"]
    before = counts(w)
    with pytest.raises(Refused) as exc:
        attach(setup, repo_root=root)
    assert exc.value.code in ("bad_state", "conflict")
    assert counts(w) == before


@pytest.mark.parametrize("failure", ["verify", "state", "record", "unverified"])
def test_partial_failure_is_atomic(setup, failure):
    w = setup[0]
    original = w.store._record_evidence
    if failure == "state":
        w.store.conn.execute("UPDATE items SET state='open' WHERE key='demo:C2'")
        w.store.conn.commit()
    elif failure == "record":
        def record(principal, item, *args):
            if item["key"] == "demo:C2":
                raise Refused("conflict", "injected")
            return original(principal, item, *args)
        w.store._record_evidence = record
    else:
        def verify(item, kind, ref, now):
            if item["key"] == "demo:C2":
                if failure == "verify":
                    raise Refused("bad_state", "injected")
                return Result(False, "injected", ref)
            return Result(True, "ok", ref)
        w.store._verify_evidence = verify
    before = counts(w)
    with pytest.raises(Refused):
        attach(setup)
    assert counts(w) == before


def test_real_merge_binding_accepts(setup):
    attach(setup, repo_root=setup[1])
    assert counts(setup[0])[0] == 2
