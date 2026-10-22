from __future__ import annotations

import subprocess

import pytest

from door_util import Doors
from keeper_util import World, defn
from plangraph.keeper import api_routes
from plangraph.planner import gitenv

def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )

def make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "checkout", "-B", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("A")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "A")
    main_tip = gitenv.rev_parse(root, "refs/heads/main")
    _git(root, "checkout", "-b", "phase/demo-PH-X")
    (root / "b.txt").write_text("B")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "B")
    phase_tip = gitenv.rev_parse(root, "refs/heads/phase/demo-PH-X")
    _git(root, "checkout", "main")
    return root, main_tip, phase_tip

@pytest.fixture
def w(tmp_path):
    return World(tmp_path / "keeper")

@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()

@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)

@pytest.fixture
def gate(w):
    w.store.create_items(
        w.p("router"),
        [
            defn("C1", [], kind="lane"),
            defn("C2", [], kind="lane"),
            defn("PH-X", ["C1", "C2"], kind="gate"),
            defn("GOAL", ["PH-X"], kind="goal"),
        ],
    )
    for key in ("demo:C1", "demo:C2"):
        w.store.put_card(
            w.p("router"),
            key,
            {
                "outcome": "do the thing",
                "check_cmd": "test -f x.txt",
                "write_set": ["x.txt"],
            },
        )

def _code(body):
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, str):
            return err
        if isinstance(err, dict):
            return err.get("code")
        return body.get("code")
    return None

def _post(doors, principal, body):
    return doors.http(principal, "POST", "/v1/worker_claim", body)

def test_registered():
    assert "worker_claim" in api_routes.WRITE_VERBS
    assert "worker_claim" in api_routes.ROLE_CHECKED_VERBS
    assert "worker_claim" not in api_routes.ROUTER_VERBS

def test_worker_claim_ok(doors, w, repo, gate, monkeypatch):
    root, _main_tip, phase_tip = repo
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    status, body = _post(doors, "workerA", {"card": "demo:C1"})
    assert status == 200
    assert body["card"] == "demo:C1"
    assert body["base_sha"] == phase_tip
    assert body["repo"] == str(root)
    assert body["check_cmd"] == "test -f x.txt"
    assert body["allow"] == ["x.txt"]
    assert body["forbid"] == []

def test_router_forbidden_without_env(doors, w, gate, monkeypatch):
    monkeypatch.delenv("PG_PHASE_REPO_ROOT", raising=False)
    status, body = _post(doors, "router", {"card": "demo:C1"})
    assert status == 403
    assert _code(body) == "forbidden"

def test_worker_unavailable_without_env(doors, w, gate, monkeypatch):
    monkeypatch.delenv("PG_PHASE_REPO_ROOT", raising=False)
    status, body = _post(doors, "workerA", {"card": "demo:C1"})
    assert status == 503
    assert _code(body) == "unavailable"

def test_body_repo_is_ignored(doors, w, repo, gate, monkeypatch):
    root, _main_tip, _phase_tip = repo
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    status, body = _post(
        doors,
        "workerA",
        {"card": "demo:C1", "repo": "/elsewhere"},
    )
    assert status == 200
    assert body["repo"] == str(root)

def test_body_worker_mismatch_is_400(doors, w, repo, gate, monkeypatch):
    root, _main_tip, _phase_tip = repo
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    status, body = _post(
        doors,
        "workerA",
        {"card": "demo:C1", "worker": "workerB"},
    )
    assert status == 400
    assert _code(body) == "validation"

def test_worker_b_after_worker_a_is_conflict(doors, w, repo, gate, monkeypatch):
    root, _main_tip, _phase_tip = repo
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    status1, _body1 = _post(doors, "workerA", {"card": "demo:C1"})
    assert status1 == 200
    status2, body2 = _post(doors, "workerB", {"card": "demo:C1"})
    assert status2 == 409
    assert _code(body2) == "conflict"

def test_v1_claim_is_not_worker_claim(doors, w, repo, gate, monkeypatch):
    root, _main_tip, _phase_tip = repo
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    status, body = doors.http(
        "workerA",
        "POST",
        "/v1/claim",
        {"worker": "workerA", "card": "demo:C1"},
    )
    assert not (status == 200 and isinstance(body, dict) and "base_sha" in body)
