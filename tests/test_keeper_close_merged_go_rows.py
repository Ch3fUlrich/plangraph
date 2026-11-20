"""LE-CLOSE-MERGED-REFUSES-VALID-GO-ROWS: real GO rows bind to the merged PR head they name."""
import pytest

from keeper_util import World, defn, to_in_review
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter
from test_keeper_merge_close_auto import Repo, git

# The decision cells as the router wrote them (trimmed after the part the parser reads);
# {head} stands for the GO-HEAD sha, which a test cannot choose for a commit it makes.
ROWS = {
    "D-5826": ("plangraph:SPAWNER-LEG-SNAPSHOT-S1-READ-ERROR", 841,
               "| D-5826 | plangraph | (17:22Z, date -u) GO plangraph PR #841 SPAWNER-LEG-SNAPSHOT-S1-READ-ERROR"
               " at GO-HEAD {head} (D-5824 lane, plan-order-0493 merges): 8 files +329/-41, MERGEABLE CLEAN, CI run"
               " 43469218982 success at that head (gh). Last delta 48a8b2cc..{short} = docs SPAWNER.md +1/-1, no source"
               " change; seat mimo-v2.6-flash run-e5c3f740 VERDICT ACCEPT, TEST_GAMING no, 0 HIGH 0 MED 2 LOW. Second"
               " family missing on the Claude delta: accepted under D-5824. PLAN"
               " plangraph:SPAWNER-LEG-SNAPSHOT-S1-READ-ERROR conforms=yes deviations=none |"),
    "D-5802": ("plangraph:WIN-ATOMIC-WRITE-RENAME-EX", 853,
               "| D-5802 | plangraph | (15:46Z, date -u) GO plangraph PR #853 WIN-ATOMIC-WRITE-RENAME-EX (D-5782 (1))"
               " at GO-HEAD {head}: 2 files +3/-1, MERGEABLE CLEAN, CI 11 success 2 skipped, GATE-PATHS none (prsgate)."
               " Seats: mimo run-127b16a1 ACCEPT 1 LOW (head_sha {short}, read via spawner result); claude-haiku-5.5"
               " REQUEST_CHANGES 1 HIGH on check_changelog_docs REFUTED: CI changelog-docs pass at the head. PLAN"
               " plangraph:WIN-ATOMIC-WRITE-RENAME-EX conforms=yes deviations=none |"),
    "D-5152": ("plangraph:CI-PREFLIGHT-LOWS", 738,
               "| D-5152 | plangraph | (18:5xZ) GO #738 CI-PREFLIGHT-LOWS slice 2 with --merge, merge only by"
               " plangraph-replan-0411, after #737 (both touch CONTRIBUTING.md; if GitHub then needs a rebase the head"
               " moves = new GO-REQ); GO-HEAD {head}. Seat run-815d9f38 VERDICT ACCEPT, 0 HIGH/MED, 3 LOW. Router"
               " read: 9 success + 4 skipped check-runs, MERGEABLE CLEAN, +58/-9. PLAN plangraph:CI-PREFLIGHT-LOWS"
               " conforms=yes deviations=none | routing-00 | GO-REQ from plangraph-replan-0411, checked by the router. |"),
    "D-5761": ("plangraph:SPAWN-PROVIDERS-OWN", 821,
               "| D-5761 | plangraph | (13:22Z, date -u) RULINGS. (1) GO plangraph PR 821 SPAWN-PROVIDERS-OWN slice A,"
               " mark ready and merge by plangraph-replan; GO-HEAD {head}; a moved head voids this. gh read by the"
               " router: MERGEABLE CLEAN, 16 files +305/-69, 13 checks success, no gate path; keeper tier high. Seats on"
               " delta: mimo-v2.6-flash run-f8e08bec ACCEPT 0 HIGH 0 MED 2 LOW. (2) DEFINED SPAWNER-LEG-SNAPSHOT-S1-READ-ERROR"
               " under plangraph:SPAWNER-LEG-STATE-SNAPSHOT-ERROR (#812 closed unmerged). (3) #829 close unmerged and"
               " replan in two slices. (5) #824 SEAT-RECORDS-ALWAYS-POSTABLE slice 1 at 9916cd5d: no GO yet; both seats"
               " say REQUEST_CHANGES. PLAN plangraph:SPAWN-PROVIDERS-OWN conforms=yes deviations=none | routing-0474 |"
               " plangraph-replan-0477 batch. |"),
}
OTHER = "f" * 40


@pytest.fixture
def world(tmp_path):
    (tmp_path / "w").mkdir()
    w = World(tmp_path / "w", backend="sqlite")
    spawner = auth.add_principal(w.conn, "spawner", "spawner", [])
    w.principals["spawner"] = auth.authenticate(w.conn, spawner)
    w.repos, w.dir = {}, tmp_path
    yield w
    w.close()


def repo(w, name):
    (w.dir / name).mkdir(exist_ok=True)
    w.repos[name] = Repo(w.dir / name) if name not in w.repos else w.repos[name]
    return w.repos[name]


def setup(w, key, **fields):
    bare = key.split(":")[1]
    w.store.create_items(w.p("router"), [defn(bare, project="plangraph", **{"review_tier": "standard", **fields}),
                                         defn("GOAL", [bare], kind="goal", project="plangraph")])
    to_claimed_as_l1p(w, key)
    return key


def to_claimed_as_l1p(w, key):
    token = auth.add_principal(w.conn, f"l1-{key.split(':')[1][:8]}", "l1", ["plangraph"], family="teamA")
    who = auth.authenticate(w.conn, token)
    epoch = w.store.claim(who, key, lease_s=7200)["epoch"]
    w.store.add_evidence(who, key, epoch, "decision", {"id": "D-1"})
    w.store.submit(who, key, epoch)


def finish(w, key, decision, head_row):
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "fixture-seats", "phase": "finished", "rc": 0,
        "seats": [{"model": "gemini-test", "verdict": "pass"}]})
    path = w.dir / "DECISIONS.md"
    path.write_text(head_row + "\n", encoding="utf-8")
    w.store.ctx = {"decisions": DecisionsAdapter(path), "git": GitAdapter(
        {name: str(r.root) for name, r in w.repos.items()} | {"plangraph": str(w.repos["plangraph"].root)})}
    return w.store.close_merged(w.p("router"), key, decision, ["google"])


def own_merge(w, key, pr):
    return repo(w, "plangraph").land(f"pr{pr}", f"Plan-Item: {key}", pr)


def row_for(decision, head):
    key, pr, template = ROWS[decision]
    return template.format(head=head, short=head[:8])


@pytest.mark.parametrize("decision", sorted(ROWS))
def test_a_row_binds_to_the_merged_pr_head_it_names(world, decision):
    key, pr, _ = ROWS[decision]
    setup(world, key)
    head = own_merge(world, key, pr)
    assert finish(world, key, decision, row_for(decision, head))["state"] == "done"


@pytest.mark.parametrize("decision", sorted(ROWS))
def test_a_row_binds_when_the_trailer_search_finds_another_merge(world, decision):
    """The item's Plan-Item commits were merged by another PR; the row names the PR that merged its head."""
    key, pr, _ = ROWS[decision]
    setup(world, key)
    repo(world, "plangraph").land("earlier", f"Plan-Item: {key}", 900)
    head = repo(world, "plangraph").land(f"pr{pr}", "no trailer here", pr)
    assert finish(world, key, decision, row_for(decision, head))["state"] == "done"


@pytest.mark.parametrize("decision", sorted(ROWS))
def test_a_row_naming_another_head_is_refused_and_names_both_heads(world, decision):
    key, pr, _ = ROWS[decision]
    setup(world, key)
    head = own_merge(world, key, pr)
    with pytest.raises(Refused) as caught:
        finish(world, key, decision, row_for(decision, OTHER))
    assert caught.value.code == "evidence_unverified"
    assert "go_row_head_mismatch" in str(caught.value)
    assert OTHER[:12] in str(caught.value) and head[:12] in str(caught.value)
    assert world.store._item(key)["state"] == "in_review"


def test_a_row_naming_a_pr_whose_head_is_not_the_named_head_is_refused(world):
    decision = "D-5802"
    key, pr, _ = ROWS[decision]
    setup(world, key)
    own_merge(world, key, 111)  # the item merged through #111, the row names #853 at a head no merge has
    with pytest.raises(Refused, match="go_row_head_mismatch"):
        finish(world, key, decision, row_for(decision, OTHER))


def test_an_item_with_a_repository_hint_is_verified_in_that_repository(world):
    decision = "D-5802"
    key, pr, _ = ROWS[decision]
    setup(world, key, description="Merge-Repo: autoos")
    repo(world, "plangraph")
    head = repo(world, "autoos").land(f"pr{pr}", f"Plan-Item: {key}", pr)
    assert finish(world, key, decision, row_for(decision, head))["state"] == "done"


def test_an_item_without_the_hint_is_not_verified_in_the_other_repository(world):
    decision = "D-5802"
    key, pr, _ = ROWS[decision]
    setup(world, key)
    repo(world, "plangraph")
    head = repo(world, "autoos").land(f"pr{pr}", f"Plan-Item: {key}", pr)
    with pytest.raises(Refused, match="merged_on_default_missing"):
        finish(world, key, decision, row_for(decision, head))


@pytest.mark.parametrize("row", [
    "| D-5713 | plangraph | (11:01Z, date -u) RULING on a BLOCKED line for plangraph:FU-WHYHOW-DECISIONS (#804): the item"
    " closes on the two verdicts the GO D-5710 already counted. |",
    "| D-2770 | plangraph | REVIEW-METHOD-SETTING accepted (HIGH): #185 d85d38ff all on origin/main (verified ancestry,"
    " both PRs MERGED); seats qwen APPROVE. Closes: plangraph:REVIEW-METHOD-SETTING@d85d38ff. |"])
def test_a_ruling_or_an_old_format_acceptance_row_is_not_a_go_and_says_so(world, row):
    key = "plangraph:FU-WHYHOW-DECISIONS" if "D-5713" in row else "plangraph:REVIEW-METHOD-SETTING"
    setup(world, key)
    repo(world, "plangraph").land("pr804", f"Plan-Item: {key}", 804)
    with pytest.raises(Refused, match="go_row_not_go"):
        finish(world, key, row.split("|")[1].strip(), row)
    assert world.store._item(key)["state"] == "in_review"
