"""Principals and tokens: sha256-only storage, revoke, unknown token."""
import hashlib

import pytest

from keeper_util import World
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def test_token_is_shown_once_and_hashed(w):
    stored = w.conn.execute(
        "SELECT token_sha256 FROM principals WHERE name='l1main'").fetchone()[0]
    assert stored == hashlib.sha256(w.tok("l1main").encode()).hexdigest()
    assert w.tok("l1main") not in stored


def test_authenticate_round_trip(w):
    principal = auth.authenticate(w.conn, w.tok("router"))
    assert principal.name == "router" and principal.role == "router"


def test_unknown_token_refused(w):
    with pytest.raises(Refused) as exc:
        auth.authenticate(w.conn, "not-a-real-token")
    assert exc.value.code == "forbidden"


def test_revoked_token_refused(w):
    auth.revoke(w.conn, "workerA")
    with pytest.raises(Refused) as exc:
        auth.authenticate(w.conn, w.tok("workerA"))
    assert exc.value.code == "forbidden"


def test_duplicate_principal_conflict(w):
    with pytest.raises(Refused) as exc:
        auth.add_principal(w.conn, "l1main", "l1", ["demo"])
    assert exc.value.code == "conflict"


def test_bad_role_validation(w):
    with pytest.raises(Refused) as exc:
        auth.add_principal(w.conn, "weirdo", "overlord", [])
    assert exc.value.code == "validation"


def test_tokens_are_unique(w):
    first = auth.add_principal(w.conn, "dup1", "worker", [])
    second = auth.add_principal(w.conn, "dup2", "worker", [])
    assert first != second
