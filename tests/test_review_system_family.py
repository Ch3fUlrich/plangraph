"""System claims derive review independence from real evidence commit writers."""
from __future__ import annotations

import json

import pytest

from evidence_fakes import make_ctx
from keeper_util import World, seed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from test_evidence_git_writer_trailers import (
    _commit, hermetic_git, repo,  # noqa: F401 - shared hermetic real-git fixtures
)


@pytest.fixture
def w(tmp_path, repo):
    world = World(tmp_path)
    world.store.ctx = make_ctx(git=GitAdapter({"demo": str(repo)}))
    for name, family in (("same", "xiaomi"), ("other", "google")):
        token = auth.add_principal(world.conn, name, "reviewer", ["demo"],
                                   family=family)
        world.principals[name] = auth.authenticate(world.conn, token)
    yield world
    world.close()


def system_card(w, repo, writer):
    (key,) = seed(w, [("A", [])], review_tier="high")
    # Seed the post-worker_complete state and already-verified evidence. No
    # injected verifier participates in the git lookup being tested here.
    w.conn.execute("UPDATE items SET claimed_by='system', state='in_review'"
                   " WHERE key=?", (key,))
    if writer is not None:
        trailers = {"Plan-Item": key}
        if writer:
            trailers["Writer"] = writer
        sha = _commit(repo, "worker evidence", trailers)
        w.conn.execute(
            "INSERT INTO evidence (key, epoch, kind, ref, verified, detail,"
            " at) VALUES (?, 0, 'commit+ci', ?, 1, '', ?)",
            (key, json.dumps({"commit": sha}), w.store._iso(w.clock())))
    return key


@pytest.mark.parametrize("writer,reviewer,code", [
    ("mimo-v2 run1", "same", "same_family"),
    ("mimo-v2 run1", "other", None),
    ("", "other", "writer_unknown"),
    (None, "other", "writer_unknown"),
    ("unknown-model run1", "other", "writer_unknown"),
])
def test_system_review_uses_evidence_writers(w, repo, writer, reviewer, code):
    key = system_card(w, repo, writer)
    assert w.p("system").family is None
    if code:
        with pytest.raises(Refused) as exc:
            w.store.verdict(w.p(reviewer), key, True, "reviewed")
        assert exc.value.code == code
    else:
        assert w.store.verdict(w.p(reviewer), key, True,
                               "reviewed")["state"] == "in_review"
