"""D-852 hermetic regressions for the router GO close (D-5504/D-5506)."""
from contextlib import contextmanager
from itertools import count
from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, defn, to_in_review
from plangraph.keeper import auth, events, mcp
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence import DecisionVerifier
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "demo:A"
HEAD = "abcdef0123456789" * 2 + "abcdef01"
MERGE = "1" * 40


class MergedGit:
    def repo_path(self, project):
        assert project == "demo"
        return "fixture-repo"

    def repo_ref(self, project):
        return "main"

    def merge_commit_of(self, repo, sha, ref):
        return (MERGE, "fixture@example.invalid") if sha == HEAD else None

    def _run(self, repo, args):
        assert args == ["show", "-s", "--format=%P", "--end-of-options", MERGE]
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {HEAD}")


@pytest.fixture
def w(tmp_path, monkeypatch):
    tokens = count()
    monkeypatch.setattr(auth, "_mint_token", lambda: f"fixture-token-{next(tokens)}")
    world = World(tmp_path, backend="sqlite")
    token = auth.add_principal(world.conn, "spawner", "spawner", [])
    world.principals["spawner"] = auth.authenticate(world.conn, token)
    world.store.create_items(world.p("router"), [
        defn("A", review_tier="standard"), defn("H", review_tier="high"),
        defn("G", ["A", "H"], kind="goal")])
    to_in_review(world, KEY)
    to_in_review(world, "demo:H")
    path = tmp_path / "DECISIONS.md"
    path.write_text(f"| D-5504 | demo | GO {HEAD[:8]} |\n", encoding="utf-8")
    world.decision_path = path
    world.store.ctx = {"decisions": DecisionsAdapter(path), "git": MergedGit()}
    world.store.verifiers["decision"] = DecisionVerifier()
    # This is exactly the keeper source used to render merged_on_default.
    from plangraph.keeper import context_sources
    world.real_plan_item_commits = context_sources.plan_item_commits
    monkeypatch.setattr(context_sources, "plan_item_commits", lambda *a, **kw: [
        {"sha": HEAD, "date": "2026-01-01", "merge": {"pr": 1}}])
    for key in (KEY, "demo:H"):
        world.store.record_run(world.p("spawner"), key, {
            "run_id": "fixture-seats", "phase": "finished", "rc": 0,
            "seats": [{"model": "gemini-test", "verdict": "pass"},
                      {"model": "deepseek-test", "verdict": "pass"}]})
    yield world
    world.close()


def _close(w, key=KEY, families=None, decision="D-5504", who="router"):
    return w.store.close_merged(w.p(who), key, decision,
                                ["google"] if families is None else families)


def _counts(w):
    return tuple(w.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("events", "evidence", "audit_log"))


def _refused(w, code, reason, **kwargs):
    before = _counts(w)
    item = w.store._item(kwargs.get("key", KEY))
    state = item["state"] if item else None
    with pytest.raises(Refused) as exc:
        _close(w, **kwargs)
    assert exc.value.code == code and reason in exc.value.msg
    after = _counts(w)
    assert after == (before[0], before[1], before[2] + 1)
    row = w.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["action"], row["ok"], row["code"]) == ("close_merged", 0, code)
    assert reason in row["msg"]
    item = w.store._item(kwargs.get("key", KEY))
    assert (item["state"] if item else None) == state


@pytest.mark.parametrize("key,families", [(KEY, [" Google "]),
                                         ("demo:H", ["google", "deepseek"])])
def test_router_closes_merged_review_with_evidence_and_one_audit(w, key, families):
    before = _counts(w)
    out = _close(w, key, families)
    assert out["state"] == "done" and out["reviews_passed"] == len(families)
    assert _counts(w) == (before[0] + 2, before[1] + 1, before[2] + 1)
    item = w.store._item(key)
    assert (item["state"], item["reviewer"]) == ("done", "router")
    record = w.store._evidence(key)[-1]
    assert record["kind"] == "decision" and record["verified"]
    assert record["epoch"] == item["epoch"]
    assert record["ref"] == {"id": "D-5504", "families": [f.strip().lower() for f in families],
                              "merged_head": HEAD, "caller": "router"}
    row = w.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["actor"], row["role"], row["action"], row["key"], row["ok"]) == (
        "router", "router", "close_merged", key, 1)
    assert row["code"] is None and row["msg"] is None
    assert events.verify_chain(w.conn) == [] and w.store.verify_integrity() == []
    _refused(w, "bad_state", "done", key=key, families=families)


@pytest.mark.parametrize("who", ["workerA", "l1main", "reviewer", "reader", "system", "spawner"])
def test_non_router_is_forbidden_even_with_malformed_arguments(w, who):
    _refused(w, "forbidden", f"role '{w.p(who).role}' may not do this",
             who=who, families=[], decision=None)


@pytest.mark.parametrize("state", ["claimed", "done", "open", "cancelled"])
def test_only_in_review_state_is_accepted(w, state):
    if state == "done":
        _close(w)
    else:
        w.conn.execute("UPDATE items SET state=? WHERE key=?", (state, KEY))
    _refused(w, "bad_state", f"is {state}, not in_review")


@pytest.mark.parametrize("commits", [None, [], [{"sha": "bad"}]])
def test_missing_merged_on_default_refuses(w, monkeypatch, commits):
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        lambda *a, **kw: commits)
    _refused(w, "evidence_unverified", "merged_on_default_missing")


def test_unknown_decision_refuses(w):
    _refused(w, "evidence_unverified", "not recorded", decision="D-9999")


@pytest.mark.parametrize("text", ["GO 12345678", f"GO {HEAD[:7]}",
                                 f"GO x{HEAD[:8]}", f"GO {HEAD}0"])
def test_go_row_head_mismatch_refuses(w, text):
    w.decision_path.write_text(f"| D-5504 | demo | {text} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("text", [f"HOLD {HEAD}", f"NO-GO {HEAD}", f"NO GO {HEAD}",
                                 f"NOT A GO {HEAD}", f"not yet a GO {HEAD}",
                                 f"title GO {HEAD}"])
def test_non_go_row_refuses(w, text):
    w.decision_path.write_text(f"| D-5504 | demo | {text} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_not_go")


def test_head_in_another_cell_cannot_authorize_close(w):
    w.decision_path.write_text(f"| D-5504 | {HEAD} | GO other head |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("families,code,reason", [
    (["google"], "coverage", "insufficient_seat_families"),
    (["google", " Google "], "same_family", "duplicate_seat_family"),
    (["google", "gemini-test"], "same_family", "duplicate_seat_family"),
    (["google", "Anthropic"], "same_family", "anthropic_seat_family"),
    (["google", "claude-sonnet"], "same_family", "anthropic_seat_family"),
    (["google", " "], "family_unknown", "seat_family_empty"),
    (["google", "foo"], "family_unknown", "seat_family_unknown"),
    (["google", "gpt-claude"], "family_unknown", "seat_family_unknown"),
    (["google", None], "family_unknown", "seat_family_empty"),
    (["google", 7], "family_unknown", "seat_family_empty"),
    ([], "coverage", "insufficient_seat_families"),
    ("google", "validation", "families must be a list"),
])
def test_invalid_seat_families_refuse(w, families, code, reason):
    _refused(w, code, reason, key="demo:H", families=families)


@pytest.mark.parametrize("source", ["writer_model", "owner_family", "claimant", "delegate", "run"])
def test_recorded_writer_family_is_excluded(w, source):
    if source == "writer_model":
        w.conn.execute("UPDATE items SET writer_model='gemini-test' WHERE key=?", (KEY,))
    elif source == "owner_family":
        w.conn.execute("UPDATE items SET owner_family='google' WHERE key=?", (KEY,))
    elif source in ("claimant", "delegate"):
        auth.add_principal(w.conn, "writerGoogle", "worker", ["demo"], family="google")
        field = "claimed_by" if source == "claimant" else "delegate"
        w.conn.execute(f"UPDATE items SET {field}='writerGoogle' WHERE key=?", (KEY,))
    else:
        w.store.record_run(w.p("spawner"), KEY, {
            "run_id": "fixture-writer", "phase": "finished", "rc": 0,
            "writer": {"family": "google", "model": "gemini-test"}})
        # Exercise the event reader, not just record_run's writer_model cache.
        w.conn.execute("UPDATE items SET writer_model=NULL WHERE key=?", (KEY,))
    _refused(w, "same_family", "writer_seat_family")


def test_absent_writer_family_does_not_invent_one(w):
    w.conn.execute("UPDATE items SET claimed_by=NULL WHERE key=?", (KEY,))
    assert _close(w)["state"] == "done"


def test_real_keeper_merged_on_default_source_is_used(w, monkeypatch):
    from plangraph.keeper import context_sources
    monkeypatch.setattr(context_sources, "plan_item_commits", w.real_plan_item_commits)

    class Git(MergedGit):
        def _run(self, repo, args):
            if args[:3] == ["show", "-s", "--format=%P"]:
                return super()._run(repo, args)
            if args[0] == "log":
                out = f"{HEAD}\x1f2026-01-01\x1fwork\x1f{KEY}\x1d"
            elif args[0] == "rev-list":
                out = "1" * 40
            else:
                out = "Merge PR #1"
            return SimpleNamespace(returncode=0, stdout=out)

    w.store.ctx["git"] = Git()
    commits = context_sources.plan_item_commits(w.store.ctx["git"], "demo", KEY, fetch=False)
    assert commits[0]["sha"] == HEAD and commits[0]["merge"]["pr"] == 1
    assert _close(w)["state"] == "done"


def test_blank_or_unavailable_decision_text_fails_closed(w):
    w.store.ctx["decisions"] = SimpleNamespace(has=lambda did: True)
    _refused(w, "evidence_unverified", "go_row_not_go")


def test_verified_commit_writer_family_is_excluded(w, monkeypatch):
    w.store.add_evidence_router(w.p("router"), KEY, "commit+ci", {"commit": HEAD})
    monkeypatch.setattr("plangraph.keeper.store_waiver.writer_families",
                        lambda *args: ["google"])
    _refused(w, "same_family", "writer_seat_family")


def test_go_row_full_uppercase_head_is_accepted(w):
    w.decision_path.write_text(f"| D-5504 | demo | GO `{HEAD.upper()}` |\n", encoding="utf-8")
    assert _close(w)["state"] == "done"


def test_no_seat_waiver_reduces_the_high_tier_gate(w):
    w.conn.execute("UPDATE items SET review_waiver='D-5504' WHERE key='demo:H'")
    _refused(w, "coverage", "insufficient_seat_families", key="demo:H")


def test_proofs_are_rechecked_inside_the_write_lock(w, monkeypatch):
    original = w.store._write

    @contextmanager
    def swapped():
        w.decision_path.write_text("| D-5504 | demo | GO 12345678 |\n", encoding="utf-8")
        with original():
            yield

    monkeypatch.setattr(w.store, "_write", swapped)
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.fixture
def doors(w, tmp_path):
    door = Doors(w, tmp_path)
    yield door
    door.close()


def test_http_close_merged_door(w, doors):
    for body in ({}, {"key": KEY, "families": []},
                 {"key": [], "decision_id": {}, "families": None}):
        status, response = doors.http("workerA", "POST", "/v1/close_merged", body)
        assert status == 403 and response["error"] == "forbidden"
    before = _counts(w)
    status, response = doors.http("router", "POST", "/v1/close_merged",
                                  {"key": KEY, "decision_id": "D-5504", "families": ["google"]})
    assert status == 200 and response["state"] == "done"
    assert _counts(w) == (before[0] + 2, before[1] + 1, before[2] + 1)


def test_cli_close_merged_door(w, doors):
    code, _, err = doors.pg("workerA", "close-merged", KEY, "D-5504", "--family", "google")
    assert code == 2 and "forbidden" in err
    code, out, err = doors.pg("router", "close-merged", "demo:H", "D-5504",
                              "--family", "google", "--family", "deepseek")
    assert code == 0, err
    assert "demo:H" in out and "done" in out


def test_mcp_close_merged_door_and_schema(w, doors, tmp_path):
    tool = next(t for t in mcp.TOOLS if t["name"] == "close_merged")
    assert tool["inputSchema"]["required"] == ["key", "decision_id", "families"]
    assert tool["inputSchema"]["properties"]["families"]["type"] == "array"
    for who, error in (("workerA", True), ("router", False)):
        token_path = tmp_path / f"fixture-{who}.token"
        assert w.tok(who).startswith("fixture-token-")
        token_path.write_text(w.tok(who), encoding="utf-8")
        response = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "close_merged", "arguments": {
                                   "key": KEY, "decision_id": "D-5504", "families": ["google"]}}},
                              mcp.HttpKeeper(doors.url, str(token_path)))
        assert response["result"]["isError"] is error


def test_close_merged_is_not_a_batch_op(w):
    with pytest.raises(Refused):
        w.store.apply_ops(w.p("router"), [{"op": "close_merged", "key": KEY,
                                          "decision_id": "D-5504", "families": ["google"]}])
    assert w.store._item(KEY)["state"] == "in_review"
