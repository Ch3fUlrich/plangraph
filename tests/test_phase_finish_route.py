import pytest

from plangraph.keeper import api_routes
from door_util import Doors
from keeper_util import World

from test_phase_finish import FakeGitHub, finish_cards, gate, git_repo

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

def _post(doors, principal, body):
    return doors.http(principal, "POST", "/v1/phase_finish", body)

def test_registered():
    assert "phase_finish" in api_routes.WRITE_VERBS
    assert "phase_finish" in api_routes.ROLE_CHECKED_VERBS
    assert "phase_finish" not in api_routes.ROUTER_VERBS

def test_role_first_even_without_env(doors, monkeypatch):
    monkeypatch.delenv("PG_PHASE_REPO_ROOT", raising=False)
    body = {"key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": "a" * 40}
    for principal in ("workerA", "l1main"):
        status, response = _post(doors, principal, body)
        assert status == 403
        assert response["error"] == "forbidden"

def test_unavailable_without_repo_root_or_credential(doors, monkeypatch, tmp_path):
    monkeypatch.delenv("PG_PHASE_REPO_ROOT", raising=False)
    body = {"key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": "a" * 40}
    status, response = _post(doors, "router", body)
    assert status == 503
    assert response["error"] == "unavailable"

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    monkeypatch.setattr(api_routes, "_github_from_env", lambda: None)
    status, response = _post(doors, "router", body)
    assert status == 503
    assert response["error"] == "unavailable"

def test_router_lands_phase_through_the_door(doors, w, tmp_path, monkeypatch):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    fake = FakeGitHub()
    monkeypatch.setattr(api_routes, "_github_from_env", lambda: fake)

    body = {"key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": head}
    status, response = _post(doors, "router", body)
    assert status == 200
    assert response["state"] == "merged"
    assert response["pr"]["number"] == 7
    assert response["merge_sha"] == fake.merge_sha

    fake.pr = {"number": 7, "url": "https://example.invalid/pr/7", "state": "merged", "head": head,
               "merge_sha": fake.merge_sha}
    status, response = _post(doors, "router", body)
    assert status == 200
    assert response["state"] == "merged"
    assert response["pr"]["number"] == 7

    merge_calls = [call for call in fake.calls if "merge_pr" in str(call)]
    assert len(merge_calls) == 1

def test_body_cannot_redirect_repo_or_credential(doors, w, tmp_path, monkeypatch):
    gate(w)
    finish_cards(w, "demo:C1", "demo:C2")
    root, head = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    fake = FakeGitHub()
    monkeypatch.setattr(api_routes, "_github_from_env", lambda: fake)

    body = {
        "key": "demo:PH-X",
        "branch": "phase/demo-PH-X",
        "head": head,
        "repo_root": str(tmp_path / "elsewhere"),
        "token_file": str(tmp_path / "token"),
        "repo": "elsewhere/other",
    }
    status, response = _post(doors, "router", body)
    assert status == 200
    assert response["state"] == "merged"
    assert response["pr"]["number"] == 7

    push = next(c for c in fake.calls if c[0] == "push_branch")
    assert response["merge_sha"] == fake.merge_sha
    assert str(push[1]) == str(root)

def test_not_done_cards_is_409_or_validation_mapped(doors, w, tmp_path, monkeypatch):
    gate(w)
    root, head = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    fake = FakeGitHub()
    monkeypatch.setattr(api_routes, "_github_from_env", lambda: fake)

    body = {"key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": head}
    status, response = _post(doors, "router", body)
    assert response["error"] == "bad_state"
    assert status in (409, 422, 400)
