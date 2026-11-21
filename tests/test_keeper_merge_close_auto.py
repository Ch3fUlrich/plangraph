"""MERGE-CLOSES-ITEMS-NO-AGENT-CALL: a merge into main closes its keeper items by code."""
import json
import subprocess

import pytest

from keeper_util import World, defn, to_claimed, to_in_review
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter


def git(root, *args, message=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=keeper", "-c", "user.email=keeper@example.invalid",
         *args], cwd=root, input=message, text=True).strip()


class Repo:
    def __init__(self, root):
        self.root = root
        git(root, "init", "-q", "-b", "main")
        git(root, "commit", "--allow-empty", "-q", "-m", "base")
        self.heads = {}

    def land(self, name, trailers, pr, *, merge=True):
        """Commit ``trailers`` on a branch and merge it as PR ``pr`` (no-ff)."""
        git(self.root, "checkout", "-q", "-b", name, "main")
        git(self.root, "commit", "--allow-empty", "-q", "-m", f"work {name}\n\n{trailers}\n")
        head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "main")
        if merge:
            git(self.root, "merge", "--no-ff", "-q", "-m", f"Merge pull request #{pr} from x/{name}", name)
            git(self.root, "branch", "-q", "-D", name)
        self.heads[name] = head
        return head


def seat_record(root, run, key, head, model="gemini-test"):
    path = root / "fixture" / run / "record.json"
    path.parent.mkdir(parents=True, mode=0o700)
    for parent in (root, root / "fixture"):
        parent.chmod(0o700)
    path.write_text(json.dumps({
        "run_id": run, "host": "fixture", "item_key": key, "role": "review",
        "status": "completed", "exit_code": 0, "served_model": model,
        "finish_reason": "stop", "attempts": [{}], "completion": {"head_sha": head},
        "stdout_tail": "TEST_GAMING: no\nVERDICT: PASS\n"}), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_mint_token", iter(f"fixture-token-{i}" for i in range(99)).__next__)
    (tmp_path / "w").mkdir()
    world = World(tmp_path / "w", backend="sqlite")
    router = world.p("router")
    world.store.create_items(router, [defn(i) for i in "ABCPSNH"] + [
        defn("G", list("ABCPSNH"), kind="goal")])
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    spawner_token = auth.add_principal(world.conn, "spawner", "spawner", [])
    world.principals["spawner"] = auth.authenticate(world.conn, spawner_token)
    world.store.ctx = {"git": GitAdapter({"demo": str(repo.root)}),
                       "spawner_records": SpawnerRecordAdapter(tmp_path / "mirror")}
    world.repo, world.mirror = repo, tmp_path / "mirror"
    yield world
    world.close()


def set_states(env):
    to_claimed(env, "demo:B")
    to_in_review(env, "demo:C")
    to_in_review(env, "demo:S")
    to_in_review(env, "demo:N")
    env.store.put_card(env.p("router"), "demo:S", {"outcome": "fixture", "risk_tags": ["auth"]})


def seat(env, key, head, run=None):
    run = run or f"run-{key[-1].lower()}"
    seat_record(env.mirror, run, key, head)
    env.store.record_run(env.p("spawner"), key, {
        "run_id": run, "phase": "finished", "rc": 0,
        "seats": [{"model": "gemini-test", "verdict": "pass", "run_id": run}]})


def auto(env, **kw):
    return env.store.close_merged_auto(env.p("system"), **kw)


def state(env, key):
    return env.store._item(key)


def test_merge_closes_open_claimed_and_in_review_items_without_a_router(env):
    set_states(env)
    for key, pr in (("A", 11), ("B", 12), ("C", 13)):
        head = env.repo.land(key.lower(), f"Plan-Item: demo:{key}", pr)
        if key != "A":  # an open item has no run to record: it closes with seats off
            seat(env, f"demo:{key}", head)
    first = auto(env)
    assert [c["key"] for c in first["closed"]] == ["demo:B", "demo:C"]
    assert [r["key"] for r in first["listed"]] == ["demo:A"]
    result = auto(env, require_seats=False)
    result["closed"] = first["closed"] + result["closed"]
    assert [c["key"] for c in result["closed"]] == ["demo:B", "demo:C", "demo:A"]
    for key in ("demo:A", "demo:B", "demo:C"):
        item = state(env, key)
        assert (item["state"], item["claimed_by"], item["delegate"], item["lease_until"]) == (
            "done", None, None, None)
        assert item["reviewer"] == "system"
        kinds = env.conn.execute("SELECT kind, verified FROM evidence WHERE key=?", (key,)).fetchall()
        assert ("commit", 1) in [tuple(r) for r in kinds]
    assert env.store.verify_integrity() == []
    assert result["merged_not_done"] == 0
    assert auto(env)["closed"] == []


def test_a_plan_item_part_key_stays_open_and_unmerged_keys_are_not_listed(env):
    env.repo.land("p", "Plan-Item-Part: demo:P", 21)
    env.repo.land("x", "Plan-Item: demo:B", 22)
    to_in_review(env, "demo:N")
    result = auto(env, require_seats=False)
    assert [c["key"] for c in result["closed"]] == ["demo:B"]
    assert state(env, "demo:P")["state"] == "open"
    assert state(env, "demo:N")["state"] == "in_review"
    assert result["listed"] == []


def test_a_sensitive_key_is_listed_with_its_reason_and_not_closed(env):
    set_states(env)
    head = env.repo.land("s", "Plan-Item: demo:S", 31)
    seat(env, "demo:S", head)
    result = auto(env)
    assert state(env, "demo:S")["state"] == "in_review"
    assert [(r["key"], r["reason"].split(":")[0]) for r in result["listed"]] == [
        ("demo:S", "forbidden")]
    assert "sensitive" in result["listed"][0]["reason"]
    assert result["merged_not_done"] == 1


def test_require_seats_on_lists_a_merge_without_a_seat_run_and_off_closes_it(env):
    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 41)
    on = auto(env)
    assert state(env, "demo:C")["state"] == "in_review"
    assert [r["key"] for r in on["listed"]] == ["demo:C"] and "NO-WITNESS" in on["listed"][0]["reason"]
    off = auto(env, require_seats=False)
    assert [c["key"] for c in off["closed"]] == ["demo:C"] and off["listed"] == []


def test_dry_run_changes_nothing_and_counts_the_merged_not_done(env):
    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 51)
    before = env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = auto(env, require_seats=False, apply=False)
    assert [c["key"] for c in result["would_close"]] == ["demo:C"]
    assert result["merged_not_done"] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_an_item_with_an_open_lane_stays_open_and_is_listed(env):
    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 61)
    env.repo.land("c-more", "Plan-Item: demo:C", 0, merge=False)
    git(env.repo.root, "branch", "-f", "c-more", env.repo.heads["c-more"])
    result = auto(env, require_seats=False)
    assert state(env, "demo:C")["state"] == "in_review"
    assert "unmerged_work_or_lanes_unknown" in result["listed"][0]["reason"]


def test_only_system_and_router_may_run_it_and_flags_must_be_booleans(env):
    with pytest.raises(Refused) as caught:
        auto_as(env, "l1main")
    assert caught.value.code == "forbidden"
    with pytest.raises(Refused) as caught:
        env.store.close_merged_auto(env.p("system"), require_seats="yes")
    assert caught.value.code == "validation"


def auto_as(env, who):
    return env.store.close_merged_auto(env.p(who))


def test_the_http_route_passes_the_switches_and_admits_system(env):
    from plangraph.keeper import api_routes

    assert "close_merged_auto" in api_routes.WRITE_VERBS
    route = api_routes.ROLE_CHECKED_VERBS["close_merged_auto"]
    env.repo.land("c", "Plan-Item: demo:B", 71)
    result = route(env.store, env.p("system"), {"require_seats": False, "apply": False}, None)
    assert [c["key"] for c in result["would_close"]] == ["demo:B"]
    assert state(env, "demo:B")["state"] == "open"


def two_repos(env, scan=None):
    """Project ``demo`` has its repository and the keeper names a second one, ``data``."""
    other = env.repo.root.parent / "data"
    other.mkdir()
    env.data_repo = Repo(other)
    env.store.ctx["git"] = GitAdapter({"demo": str(env.repo.root), "data": str(other)}, merge_scan=scan)
    return env.data_repo


def test_a_merge_in_another_scanned_repository_closes_the_item_and_the_repos_are_printed(env):
    set_states(env)
    data = two_repos(env)
    # the real shape: a merge commit without trailers, the trailers on the branch commits
    data.land("w1", "Plan-Item: demo:C", 400)
    data.land("w2", "Plan-Item: demo:B", 401)
    assert auto(env, repos_only=True) == {"repos": ["data", "demo"]}
    result = auto(env, require_seats=False)
    assert sorted(c["key"] for c in result["closed"]) == ["demo:B", "demo:C"]
    assert result["repos"] == ["data", "demo"] and result["listed"] == []
    assert state(env, "demo:C")["state"] == "done"


def test_the_router_verb_also_finds_the_merge_in_another_scanned_repository(env):
    set_states(env)
    data = two_repos(env)
    head = data.land("w1", "Plan-Item: demo:C", 400)
    env.store.record_run(env.p("spawner"), "demo:C", {
        "run_id": "fixture-seats", "phase": "finished", "rc": 0,
        "seats": [{"model": "gemini-test", "verdict": "pass"}]})
    path = env.repo.root.parent / "DECISIONS.md"
    path.write_text(f"| D-1 | demo | GO {head} |\n", encoding="utf-8")
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    env.store.ctx["decisions"] = DecisionsAdapter(path)
    assert env.store.close_merged(env.p("router"), "demo:C", "D-1", ["google"])["state"] == "done"


def test_a_merge_scan_list_limits_the_repositories_read(env):
    set_states(env)
    two_repos(env, scan=["demo"]).land("w1", "Plan-Item: demo:C", 400)
    result = auto(env, require_seats=False)
    assert result["repos"] == ["demo"] and result["closed"] == []
    assert state(env, "demo:C")["state"] == "in_review"


def test_a_key_merged_in_two_scanned_repositories_is_listed_not_guessed(env):
    set_states(env)
    data = two_repos(env)
    data.land("w1", "Plan-Item: demo:C", 400)
    env.repo.land("w1", "Plan-Item: demo:C", 11)
    result = auto(env, require_seats=False)
    assert result["closed"] == []
    assert "merge_repo_ambiguous: demo, data" in result["listed"][0]["reason"]
    assert state(env, "demo:C")["state"] == "in_review"


def test_the_config_names_the_scanned_repositories_in_one_place(tmp_path):
    from plangraph.keeper.config import Config, ConfigError
    repos = {"demo": str(tmp_path), "data": str(tmp_path)}
    assert Config({"repos": repos, "merge_scan": ["data"]}, tmp_path).merge_scan == ["data"]
    assert Config({"repos": repos}, tmp_path).merge_scan is None
    with pytest.raises(ConfigError):
        Config({"repos": repos, "merge_scan": ["elsewhere"]}, tmp_path)


def test_a_seat_of_the_family_that_wrote_the_landed_commit_does_not_close_it(env):
    set_states(env)
    head = env.repo.land("c", "Plan-Item: demo:C\nWriter: gemini-test session_abc123", 81)
    seat(env, "demo:C", head)      # a google seat reviews a google-written commit
    result = auto(env)
    assert state(env, "demo:C")["state"] == "in_review"
    assert "writer_seat_family" in result["listed"][0]["reason"]


def test_a_merge_repo_note_naming_another_items_merge_does_not_close_the_item(env):
    set_states(env)
    other = env.repo.land("x", "Plan-Item: demo:N", 91)
    merge = git(env.repo.root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:C'", (f"Merge-Repo: demo {merge}",))
    git(env.repo.root, "branch", "c-lane", other)  # C's own lane is still open
    result = auto(env, require_seats=False)
    assert "demo:C" not in {c["key"] for c in result["closed"]}
    assert state(env, "demo:C")["state"] == "in_review"


def test_a_merge_that_exists_only_on_origin_is_listed_with_the_configured_ref_named(env, tmp_path):
    set_states(env)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(env.repo.root), str(clone))
    env.store.ctx["git"] = GitAdapter({"demo": str(clone)})
    env.repo.land("c", "Plan-Item: demo:C", 71)   # lands after the clone: only origin/main has it
    result = auto(env, require_seats=False)
    assert result["closed"] == [] and state(env, "demo:C")["state"] == "in_review"
    assert [r["key"] for r in result["listed"]] == ["demo:C"]
    assert "merged_on_default_missing" in result["listed"][0]["reason"]


def test_a_scan_list_without_the_items_repository_closes_nothing_from_it(env):
    set_states(env)
    two_repos(env, scan=["data"])
    env.repo.land("c", "Plan-Item: demo:C", 11)
    result = auto(env, require_seats=False)
    assert result["repos"] == ["data"] and result["closed"] == []
    assert state(env, "demo:C")["state"] == "in_review"


def test_items_whose_commits_end_in_a_separate_coauthor_paragraph_close_from_a_merge_without_trailers(env):
    """The shape of #1077 and #1080: Plan-Item and Writer in one paragraph, Co-Authored-By in the last."""
    set_states(env)
    two_repos(env)
    shape = "Plan-Item: demo:{key}\nWriter: gpt-test session_abc\n\nCo-Authored-By: A <a@example.invalid>"
    env.repo.land("w1", shape.format(key="C"), 1080)
    env.data_repo.land("w2", shape.format(key="B"), 400)
    result = auto(env, require_seats=False, apply=False)
    assert sorted(c["key"] for c in result["would_close"]) == ["demo:B", "demo:C"]
    assert result["listed"] == []


def test_an_empty_scan_list_still_bounds_a_hinted_repository(env):
    set_states(env)
    other = two_repos(env, scan=[])
    head = other.land("w1", "Plan-Item: demo:C", 400)
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:C'", (f"Merge-Repo: data {head}",))
    result = auto(env, require_seats=False)
    assert result["repos"] == [] and result["closed"] == []
    assert "merge_repo_not_scanned" in result["listed"][0]["reason"]


def test_the_merge_commits_own_writer_trailer_counts_as_a_writer(env):
    set_states(env)
    head = env.repo.land("c", "Plan-Item: demo:C", 82)
    git(env.repo.root, "commit", "--amend", "-q", "-m", "Merge pull request #82\n\nPlan-Item: demo:C\nWriter: gemini-test session_abc")
    seat(env, "demo:C", head)
    result = auto(env)
    assert state(env, "demo:C")["state"] == "in_review"
    assert "writer_seat_family" in result["listed"][0]["reason"]


def test_a_warmed_scan_is_not_fetched_again_under_the_lock(env, monkeypatch):
    from plangraph.keeper import context_sources

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 83)
    calls = []
    real = context_sources.work_index
    monkeypatch.setattr(context_sources, "work_index",
                        lambda git, project, *, fresh=False, max_age=None: calls.append(fresh) or real(git, project, fresh=fresh))
    env.store.warm_merge_scan()
    assert calls == [True]
    result = auto(env, require_seats=False)
    assert calls == [True, False] and [c["key"] for c in result["closed"]] == ["demo:C"]


def test_over_http_the_scan_is_warmed_before_the_request_lock_and_the_verb_closes(env, tmp_path, monkeypatch):
    from door_util import Doors

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 84)
    doors = Doors(env, tmp_path)
    held = []
    real = env.store.warm_merge_scan
    monkeypatch.setattr(env.store, "warm_merge_scan",
                        lambda *a: held.append(doors.server.lock._is_owned()) or real(*a))
    try:
        status, body = doors.http("system", "POST", "/v1/close_merged_auto", {"require_seats": False})
        assert status == 200 and [c["key"] for c in body["closed"]] == ["demo:C"]
        assert held == [False]
        assert doors.http("l1main", "POST", "/v1/close_merged_auto", {})[0] == 403
        assert held == [False]  # a non-system caller never starts the fetch
    finally:
        doors.close()


def test_a_failed_refresh_does_not_leave_the_earlier_warm_snapshot_valid(env, monkeypatch):
    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 85)
    env.store.warm_merge_scan()
    assert env.store._warm("demo")
    monkeypatch.setattr(type(env.store), "_refresh_merged_default_ref", staticmethod(lambda git, project: False))
    env.store.warm_merge_scan()
    assert not env.store._warm("demo")
    result = auto(env, require_seats=False)
    assert result["closed"] == [] and "merged_default_ref_refresh_failed" in result["listed"][0]["reason"]


def test_the_hosted_lane_lookup_is_done_while_warming_and_not_again_under_the_lock(env, tmp_path, monkeypatch):
    from plangraph.keeper import context_sources

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 86)
    env.repo.land("c-lane", "side work", 0, merge=False)  # an unmerged lane of C: a closed PR is behind it
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(env.repo.root), str(bare))
    git(env.repo.root, "remote", "add", "origin", str(bare))
    asked = []
    monkeypatch.setattr(context_sources, "_github_repo", lambda origin: "o/r")
    monkeypatch.setattr(context_sources, "_closed_lane_pr", lambda *a: asked.append(a[3]) or 77)
    real = context_sources.work_index
    monkeypatch.setattr(context_sources, "work_index", lambda git, project, **kw: dict(
        real.__wrapped__(git, project, **kw) if hasattr(real, "__wrapped__") else {
            "commits": {"demo:C": [{"sha": "a" * 40}]}, "origin_stale": False, "prs": {}, "branches": [], "order": {}}))
    env.store.warm_merge_scan(env.store.merge_candidates())
    during_warm = list(asked)
    assert during_warm, "the warm step looks the hosted lanes up"
    result = auto(env, require_seats=False)
    assert asked == during_warm and [c["key"] for c in result["closed"]] == ["demo:C"]
