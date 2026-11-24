"""KEEPER-AUTO-CLOSE-LANDING-AUTHORITY-EVIDENCE: the automatic close records why it may close."""
import pytest

from keeper_util import to_in_review
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from test_keeper_merge_close_auto import env, git  # noqa: F401  (fixtures and helpers)

STANDING = "D-9001"
ROWS = ("| D-9001 | OPERATOR | Standing: a merged PR closes the items it names |\n"
        "| D-9002 | router | GO #31 GO-HEAD {head} demo:{key} |\n")


class FakeCI:
    """A forge: merged PRs by merge sha, check-runs by PR head sha."""

    def __init__(self):
        self.prs, self.runs, self.lookups, self.run_reads = {}, {}, [], []

    def mode(self, project):
        return "owner/demo"

    def merged_pr_for(self, project, sha, ref):
        self.lookups.append(sha)
        return self.prs.get(sha)

    def check_runs(self, repo, sha):
        self.run_reads.append(sha)
        return self.runs[sha]


def success(name):
    return {"name": name, "status": "completed", "conclusion": "success",
            "id": 1, "app": {"slug": "x"}, "check_suite": {"id": 7}}


def forge(env, tmp_path, rows=ROWS.split("\n")[0] + "\n"):
    ci = FakeCI()
    path = tmp_path / "DECISIONS.md"
    path.write_text(rows, encoding="utf-8")
    env.store.ctx = {**env.store.ctx, "ci": ci, "decisions": DecisionsAdapter(path)}
    env.store.require_seats = False
    return ci, path


def land_merge(env, ci, key, pr, name):
    head = env.repo.land(name, f"Plan-Item: demo:{key}", pr)
    merge = git(env.repo.root, "rev-parse", "main")
    ci.prs[merge] = {"number": pr, "merged_at": "2026-11-24T10:00:00Z", "head": {"sha": head}}
    ci.runs[head] = [success("lint"), {**success("tests"), "id": 2, "check_suite": {"id": 8}}]
    return head, merge


def land_squash(env, ci, key, pr, name):
    """A squash merge: one parent, the PR head is not an ancestor of main."""
    git(env.repo.root, "commit", "--allow-empty", "-q", "-m", f"work {name} (#{pr})\n\nPlan-Item: demo:{key}\n")
    squash = git(env.repo.root, "rev-parse", "main")
    head = "d" * 40
    ci.prs[squash] = {"number": pr, "merged_at": "2026-11-24T11:00:00Z", "head": {"sha": head}}
    ci.runs[head] = [success("tests")]
    return head, squash


def evidence(env, key):
    return env.store._evidence(key)[-1]["ref"]


def test_a_merged_pr_close_records_the_verified_required_checks_and_the_standing_authority(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    env.store.merge_close_decision = STANDING
    head, merge = land_merge(env, ci, "A", 31, "a")
    result = env.store.close_merged_auto(env.p("system"))
    assert [c["key"] for c in result["closed"]] == ["demo:A"]
    landing = evidence(env, "demo:A")["landing"]
    assert landing["authority"] == {"decision": STANDING, "verified": True}
    assert landing["landings"] == [{
        "pr": 31, "merge_sha": merge, "head": head, "merged_at": "2026-11-24T10:00:00Z",
        "checks": {"head": head, "verified": True, "detail": "CI green", "names": ["lint", "tests"]}}]
    assert "go_row_missing" in landing["flags"]
    assert "standing_authority_missing" not in landing["flags"]


def test_a_squash_merge_closes_with_the_pr_head_the_forge_reports(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    head, squash = land_squash(env, ci, "A", 14, "s")
    result = env.store.close_merged_auto(env.p("system"))
    assert [c["key"] for c in result["closed"]] == ["demo:A"], result["listed"]
    proof = evidence(env, "demo:A")["prs"][0]
    assert (proof["pr"], proof["head"], proof["merge_sha"]) == (14, head, squash)
    landing = evidence(env, "demo:A")["landing"]["landings"][0]
    assert landing["checks"]["verified"] and landing["checks"]["head"] == head


def test_a_landing_is_looked_up_once_and_its_checks_are_cached(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    to_in_review(env, "demo:B")
    head, merge = land_merge(env, ci, "A", 31, "a")
    land_merge(env, ci, "B", 32, "b")
    env.store.close_merged_auto(env.p("system"), project="demo", apply=False)
    first = list(ci.lookups), list(ci.run_reads)
    env.store.close_merged_auto(env.p("system"), apply=True)
    assert (ci.lookups, ci.run_reads) == first
    assert ci.run_reads.count(head) == 1


def test_a_missing_go_row_and_unverified_checks_are_flagged_without_refusing(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    head, _ = land_merge(env, ci, "A", 31, "a")
    ci.runs[head] = [{**success("tests"), "conclusion": "failure"}]
    result = env.store.close_merged_auto(env.p("system"))
    assert [c["key"] for c in result["closed"]] == ["demo:A"]
    landing = evidence(env, "demo:A")["landing"]
    assert landing["flags"]["go_row_missing"] == {"prs": [31]}
    assert landing["flags"]["required_checks_unverified"][0]["pr"] == 31
    assert landing["flags"]["standing_authority_missing"] is True
    assert landing["authority"] is None
    assert result["closed"][0]["landing_flags"] == landing["flags"]


def test_a_go_row_naming_the_landed_pr_is_recorded_and_not_flagged(env, tmp_path):
    ci, path = forge(env, tmp_path)
    head, _ = land_merge(env, ci, "A", 31, "a")
    path.write_text(ROWS.format(head=head, key="A"), encoding="utf-8")
    env.store.merge_close_decision = STANDING
    env.store.close_merged_auto(env.p("system"))
    landing = evidence(env, "demo:A")["landing"]
    assert landing["go_rows"] == [{"decision_id": "D-9002", "pr": 31}]
    assert landing["flags"] == {}


def test_an_unknown_standing_decision_is_flagged_not_trusted(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    land_merge(env, ci, "A", 31, "a")
    env.store.merge_close_decision = "D-1234"
    env.store.close_merged_auto(env.p("system"))
    landing = evidence(env, "demo:A")["landing"]
    assert landing["authority"] == {"decision": "D-1234", "verified": False}
    assert landing["flags"]["standing_authority_unverified"] == "D-1234"


def test_a_frozen_run_never_asks_the_forge(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    land_merge(env, ci, "A", 31, "a")
    env.store.__dict__["_frozen_lanes"] = True
    assert env.store._landing_for("demo", git(env.repo.root, "rev-parse", "main")) is None
    assert ci.lookups == []


@pytest.mark.parametrize("lookup", ["raises", "none"])
def test_a_forge_that_proves_nothing_keeps_a_merge_commit_close_flagged(env, tmp_path, lookup):
    ci, _ = forge(env, tmp_path)
    land_merge(env, ci, "A", 31, "a")
    if lookup == "raises":
        ci.merged_pr_for = lambda *a: (_ for _ in ()).throw(RuntimeError("secret path /x"))
    else:
        ci.prs.clear()
    result = env.store.close_merged_auto(env.p("system"))
    assert [c["key"] for c in result["closed"]] == ["demo:A"]
    landing = evidence(env, "demo:A")["landing"]
    assert landing["landings"][0]["checks"] is None
    assert landing["flags"]["required_checks_unverified"][0]["detail"] == "forge_record_unavailable"
    assert "secret" not in str(landing)


def test_the_warm_scan_reads_the_forge_so_the_frozen_close_records_it(env, tmp_path):
    ci, _ = forge(env, tmp_path)
    head, squash = land_squash(env, ci, "A", 14, "s")
    candidates = env.store.merge_candidates()
    env.store.warm_merge_scan(candidates)
    warm = list(ci.lookups)
    assert squash in warm
    result = env.store.close_merged_auto(env.p("system"), frozen=True)
    assert [c["key"] for c in result["closed"]] == ["demo:A"], result["listed"]
    assert ci.lookups == warm
    assert evidence(env, "demo:A")["landing"]["landings"][0]["head"] == head


def test_the_config_names_the_standing_decision(tmp_path):
    import json
    import os
    from plangraph.keeper import config

    def load(value):
        path = tmp_path / "keeper.json"
        path.write_text(json.dumps({"db": "k.db", "merge_close_decision": value}), encoding="utf-8")
        os.chmod(path, 0o600)
        return config.load_config(path)

    assert load("D-9001").merge_close_decision == "D-9001"
    with pytest.raises(config.ConfigError):
        load("standing")
