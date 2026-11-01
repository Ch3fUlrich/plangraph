from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from git_identity import identity_env
from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.planner import gitenv
from plangraph.planner.worker_claim import build_claim, worker_claim


def test_build_claim_preserves_gate_inputs():
    got = build_claim("demo:A", {
        "outcome": "fix", "check_cmd": "pytest tests/check.py",
        "write_set": ["src/a.py"], "check_kind": "regression",
        "check_reads": ["fixtures/input.json"], "forbid_paths": ["secrets"],
        "smoke_checks": ["pytest -q"], "exports": ["api"],
        "heldout_check": "heldout", "risk_tags": ["migration"],
    }, repo="repo", base_sha="abc")
    assert got["check_kind"] == "regression"
    assert got["check_reads"] == ["fixtures/input.json"]
    assert got["forbid"] == ["secrets", "tests/check.py", "fixtures/input.json"]
    assert got["smoke_checks"] == ["pytest -q"]
    assert got["exports"] == ["api"]
    assert got["heldout_cmd"] == "heldout"
    assert got["risk_tagged"] is True

def _code(exc: Refused):
    return getattr(exc, "code", exc.args[0] if exc.args else None)

def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env or identity_env(),
    )

def make_repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "checkout", "-B", "main")
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
    world = World(tmp_path / "keeper")
    try:
        yield world
    finally:
        world.close()

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

class ExplodingStore:
    def __getattr__(self, name):
        raise AssertionError(f"store was read via {name}")

def test_claim_exact(w, repo, gate):
    root, _main_tip, phase_tip = repo
    got = worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    assert got == {
        "card": "demo:C1",
        "repo": str(root),
        "base_sha": phase_tip,
        "check_cmd": "test -f x.txt",
        "allow": ["x.txt"],
        "forbid": [],
        "prompt": "do the thing",
        "check_kind": "change", "check_reads": [], "smoke_checks": [],
        "exports": [], "heldout_cmd": "", "risk_tagged": False,
    }

def test_system_claims_like_a_worker(w, repo, gate):
    root, _main_tip, phase_tip = repo
    got = worker_claim(w.store, w.p("system"), "demo:C1", repo_root=str(root))
    assert got["base_sha"] == phase_tip
    claimed_by = w.store.conn.execute(
        "SELECT claimed_by FROM items WHERE key = ?", ("demo:C1",)
    ).fetchone()[0]
    assert claimed_by == "system"

def test_falls_back_to_main_tip(w, repo, gate):
    root, main_tip, _phase_tip = repo
    _git(root, "branch", "-D", "phase/demo-PH-X")
    got = worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    assert got["base_sha"] == main_tip

def test_reclaim_same_worker_keeps_row(w, repo, gate):
    root, _main_tip, _phase_tip = repo
    first = worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    row1 = w.store.conn.execute("SELECT * FROM items WHERE key = ?", ("demo:C1",)).fetchone()
    second = worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    row2 = w.store.conn.execute("SELECT * FROM items WHERE key = ?", ("demo:C1",)).fetchone()
    assert first == second
    assert row1 == row2

def test_expired_own_lease_is_not_idempotent(w, repo, gate):
    root, _main_tip, _phase_tip = repo
    worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    w.store.conn.execute(
        "UPDATE items SET lease_until = ? WHERE key = ?",
        ("2000-01-01T00:00:00Z", "demo:C1"),
    )
    w.store.conn.commit()
    with pytest.raises(Refused) as exc:
        worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    assert exc.value.code == "conflict"

def test_cross_project_worker_gets_not_found(w, repo, gate):
    root, _main_tip, _phase_tip = repo
    outsider = types.SimpleNamespace(name="outsider", role="worker", projects=["other"])
    with pytest.raises(Refused) as exc:
        worker_claim(w.store, outsider, "demo:C1", repo_root=str(root))
    assert exc.value.code == "not_found"
    state = w.store.conn.execute(
        "SELECT state FROM items WHERE key = ?", ("demo:C1",)
    ).fetchone()[0]
    assert state == "open"

def test_worker_b_conflict(w, repo, gate):
    root, _main_tip, _phase_tip = repo
    worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    with pytest.raises(Refused) as ei:
        worker_claim(w.store, w.p("workerB"), "demo:C1", repo_root=str(root))
    assert _code(ei.value) == "conflict"

@pytest.mark.parametrize("name", ["router", "l1main", "reviewer", "reader"])
def test_non_worker_forbidden(w, repo, gate, name):
    root, _main_tip, _phase_tip = repo
    with pytest.raises(Refused) as ei:
        worker_claim(w.store, w.p(name), "demo:C1", repo_root=str(root))
    assert _code(ei.value) == "forbidden"

def test_role_check_precedes_store_read(w, tmp_path):
    with pytest.raises(Refused) as ei:
        worker_claim(ExplodingStore(), w.p("reader"), "demo:C1", repo_root=str(tmp_path))
    assert _code(ei.value) == "forbidden"

@pytest.mark.parametrize("missing", [None, "dir"])
def test_repo_root_unavailable(w, tmp_path, gate, missing):
    repo_root = None if missing is None else str(tmp_path / "missing")
    with pytest.raises(Refused) as ei:
        worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=repo_root)
    assert _code(ei.value) == "unavailable"

def test_empty_check_cmd_is_bad_state_without_lease(w, repo, gate):
    root, _main_tip, _phase_tip = repo
    w.store.put_card(
        w.p("router"),
        "demo:C1",
        {
            "outcome": "do the thing",
            "check_cmd": "",
            "write_set": ["x.txt"],
        },
    )
    with pytest.raises(Refused) as ei:
        worker_claim(w.store, w.p("workerA"), "demo:C1", repo_root=str(root))
    assert _code(ei.value) == "bad_state"
    state = w.store.conn.execute("SELECT state FROM items WHERE key = ?", ("demo:C1",)).fetchone()[0]
    assert state == "open"

def test_unknown_key(w, repo, gate):
    root, _main_tip, _phase_tip = repo
    with pytest.raises(Refused) as ei:
        worker_claim(w.store, w.p("workerA"), "demo:NOPE", repo_root=str(root))
    assert _code(ei.value) == "not_found"

def test_build_claim_keys_and_values():
    card = {
        "check_cmd": "test -f x.txt",
        "write_set": ["x.txt"],
        "outcome": "do the thing",
    }
    got = build_claim("demo:C1", card, repo="/repo", base_sha="abc123")
    assert set(got) == {
        "card",
        "repo",
        "base_sha",
        "check_cmd",
        "allow",
        "forbid",
        "prompt",
        "check_kind", "check_reads", "smoke_checks", "exports", "heldout_cmd",
        "risk_tagged",
    }
    assert got == {
        "card": "demo:C1",
        "repo": "/repo",
        "base_sha": "abc123",
        "check_cmd": "test -f x.txt",
        "allow": ["x.txt"],
        "forbid": [],
        "prompt": "do the thing",
        "check_kind": "change", "check_reads": [], "smoke_checks": [],
        "exports": [], "heldout_cmd": "", "risk_tagged": False,
    }

def test_gitenv_scrubs_git_dir(monkeypatch, repo):
    root, main_tip, _phase_tip = repo
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    assert "GIT_DIR" not in gitenv.git_env()
    assert gitenv.rev_parse(root, "refs/heads/main") == main_tip

def test_rev_parse_missing_ref(repo):
    root, _main_tip, _phase_tip = repo
    assert gitenv.rev_parse(root, "refs/heads/nope") is None

def test_rev_parse_uses_hooks_path(monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stdout = "abc123\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Proc()

    monkeypatch.setattr(gitenv.subprocess, "run", fake_run)
    assert gitenv.rev_parse("/tmp", "HEAD") == "abc123"
    assert calls
    assert "core.hooksPath=/dev/null" in calls[0]
