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
