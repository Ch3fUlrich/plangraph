"""Hermetic decision close proofs using a local bare origin."""
import hashlib
import subprocess
from types import SimpleNamespace

import pytest
from keeper_util import World, defn

from plangraph.keeper.errors import Refused
from plangraph.keeper.store_decision_close import LOG_PATH


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(path),
             "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    repo = tmp_path / "data"
    git(tmp_path, "clone", str(origin), str(repo))
    log = repo / LOG_PATH
    log.parent.mkdir(parents=True)
    row = "| D-5024 | CLOSE demo:A |"
    log.write_text(row + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "decision")
    git(repo, "push", "origin", "main")
    w = World(tmp_path)
    w.store.create_items(w.p("router"), [
        defn("A", review_tier="none", evidence_kinds=["decision"]),
        defn("GOAL", ["A"], kind="goal")])
    claim = w.store.claim(w.p("workerA"), "demo:A")
    w.epoch = claim["epoch"]
    w.store.add_evidence_router(w.p("router"), "demo:A", "decision", {"id": "D-5024"})
    w.store.ctx = SimpleNamespace(
        git=SimpleNamespace(repo_path=lambda key: repo if key == "decisions" else None))
    w.repo, w.log, w.row = repo, log, row
    yield w
    w.close()


def close(w, **kw):
    return w.store.close_on_decision(w.p("workerA"), "demo:A",
                                     decision_ref="D-5024", epoch=w.epoch, **kw)


def test_accept_records_hash(world):
    result = close(world)
    assert result["decision_ref"] == "D-5024"
    assert result["row_sha256"] == hashlib.sha256(world.row.encode()).hexdigest()
    event = world.conn.execute(
        "SELECT payload FROM events WHERE type='closed_on_decision'").fetchone()
    assert "D-5024" in event[0] and result["row_sha256"] in event[0]


@pytest.mark.parametrize("text", ["| D-5024 | CLOSE demo:AB |",
                                      "| D-5024 | CLOSE demo:B |",
                                      "| D-5024 | no close |", "| D-9999 | CLOSE demo:A |"])
def test_main_row_refusals(world, text):
    world.log.write_text(text + "\n")
    git(world.repo, "commit", "-am", "changed")
    git(world.repo, "push", "origin", "main")
    with pytest.raises(Refused, match="row"):
        close(world)


def test_local_and_nonmain_do_not_count(world):
    world.log.write_text("| D-5024 | no close |\n")
    git(world.repo, "commit", "-am", "main refusal")
    git(world.repo, "push", "origin", "main")
    git(world.repo, "checkout", "-b", "other")
    world.log.write_text(world.row + "\n")
    git(world.repo, "commit", "-am", "local proof")
    git(world.repo, "push", "origin", "other")
    with pytest.raises(Refused, match="exact CLOSE"):
        close(world)


def test_fetch_failure(world):
    git(world.repo, "remote", "set-url", "origin", str(world.repo / "missing"))
    with pytest.raises(Refused, match="fetch failed"):
        close(world)


def test_nonclaimant(world):
    with pytest.raises(Refused, match="current claimant"):
        world.store.close_on_decision(world.p("workerB"), "demo:A",
                                      decision_ref="D-5024", epoch=world.epoch)


def test_stale_epoch(world):
    with pytest.raises(Refused, match="epoch"):
        world.store.close_on_decision(world.p("workerA"), "demo:A",
                                      decision_ref="D-5024", epoch=world.epoch - 1)


def test_missing_evidence(world):
    with pytest.raises(Refused, match="matching decision evidence"):
        world.store.close_on_decision(world.p("router"), "demo:A", decision_ref="D-9999")


def test_api_route(world):
    from plangraph.keeper.api_routes import _close_on_decision
    result = _close_on_decision(world.store, world.p("router"),
                                {"key": "demo:A", "decision_ref": "D-5024"},
                                lambda body, key: body[key])
    assert result["state"] == "done"


def test_cli_payload(monkeypatch):
    from plangraph.keeper import cli_verbs
    seen = {}
    monkeypatch.setattr(cli_verbs, "write", lambda s, a, b, f: seen.update(b))
    cli_verbs.cmd_close(SimpleNamespace(key="demo:A", batch=None,
                                        decision_ref="D-5024", epoch=3), None)
    assert seen == {"key": "demo:A", "batch_ref": None,
                     "decision_ref": "D-5024", "epoch": 3}


def test_api_cli_missing_proof_same_refusal(world, capsys):
    from plangraph.keeper.api_routes import _close_on_decision
    from plangraph.keeper.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["close", "demo:A"])
    with pytest.raises(Refused) as exc:
        _close_on_decision(world.store, world.p("router"), {"key": "demo:A"},
                           lambda body, key: body[key])
    assert exc.value.code == "validation"
    assert exc.value.msg == "one of the arguments --decision-ref --batch is required"
    assert exc.value.msg in capsys.readouterr().err


def test_git_once_outside_write(world, monkeypatch):
    from plangraph.keeper import store_decision_close as module
    run = subprocess.run
    calls = []

    def checked(args, **kwargs):
        assert not world.conn.in_transaction
        calls.append(args[3])
        return run(args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", checked)
    close(world)
    assert calls == ["fetch", "show"]


def test_slow_git_timeout_allows_other_writes(world, tmp_path, monkeypatch):
    import threading
    from plangraph.keeper import store_decision_close as module, timing

    stub = tmp_path / "git"
    stub.write_text("#!/bin/sh\nexec /bin/sleep 10\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    run = subprocess.run
    lock = threading.Lock()
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def sleeping(args, **kwargs):
        assert not world.conn.in_transaction
        started.set()
        return run(args, **{**kwargs, "timeout": 0.5})

    def other_write():
        try:
            assert started.wait(2)
            with lock:
                with world.store._write():
                    world.conn.execute("UPDATE items SET title='concurrent' WHERE key='demo:GOAL'")
                finished.set()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module.subprocess, "run", sleeping)
    worker = threading.Thread(target=other_write)
    worker.start()
    with timing.RequestTimer(lock, "write"):
        with pytest.raises(Refused, match="fetch failed"):
            close(world)
        assert finished.is_set()
    worker.join(2)
    assert not worker.is_alive()
    assert not errors


def test_cli_batch_epoch_refused():
    from plangraph.keeper.cli_verbs import cmd_close
    with pytest.raises(ValueError, match="--epoch requires --decision-ref, not --batch"):
        cmd_close(SimpleNamespace(key="demo:A", batch="proof@sha", epoch=1), None)
