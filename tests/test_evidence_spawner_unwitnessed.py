"""SEAT-GATE-UNWITNESSED-HARNESS-DECISION (D-6762): an unwitnessed seat counts only on its recorded harness."""
import json

import pytest

from test_keeper_close_merged import HEAD, unseated_w, w  # noqa: F401 - fixtures
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter

TEXT = f"HEAD: {HEAD}\nTESTS: pytest -q tests/test_fixture.py: 3 passed\nFILES READ: src/lib.rs:3\nTEST_GAMING: no\nVERDICT: ACCEPT\n"
AGY = {"id": "agy-gemini-4-hydrogen-high", "harness": "agy", "model": "gemini-4-hydrogen-high", "family": "gemini"}
OPENCODE = {"id": "mimo-v2.6-pro", "harness": "opencode", "model": "mimo/mimo-v2.6-pro", "family": "mimo"}


def mirror(tmp_path, leg=AGY, harness="agy", text=TEXT, **changes):
    root = tmp_path / "mirror"
    path = root / "fixture" / "run-one" / "record.json"
    path.parent.mkdir(parents=True, mode=0o700)
    for parent in (root, root / "fixture"):
        parent.chmod(0o700)
    record = {"run_id": "run-one", "host": "fixture", "item_key": "demo:H", "role": "review",
              "status": "completed", "exit_code": 0, "served_model": "unwitnessed",
              "served_family": "unwitnessed", "finish_reason": "stop", "attempts": [{}],
              "completion": {"head_sha": HEAD}, "chosen_leg": leg, "harness": harness, "stdout_tail": text}
    record.update(changes)
    path.write_text(json.dumps({k: v for k, v in record.items() if v is not None}), encoding="utf-8")
    path.chmod(0o600)
    return root


def seats(root, **kw):
    return SpawnerRecordAdapter(root).review_seats({"key": "demo:H", "run_ids": '["run-one"]'}, HEAD, "", **kw)


def test_an_agy_seat_with_a_recorded_harness_counts(tmp_path):
    (seat,) = seats(mirror(tmp_path), allow_unwitnessed=True)
    assert (seat["model"], seat["harness"], seat["witness"]) == ("gemini-4-hydrogen-high", "agy", "recorded-harness")


@pytest.mark.parametrize("changes", [
    {"harness": None, "chosen_leg": {k: v for k, v in AGY.items() if k != "harness"}},   # no harness recorded anywhere
    {"harness": "unknown"},
    {"chosen_leg": {**AGY, "family": ""}},              # no family
    {"chosen_leg": {**AGY, "model": "unwitnessed"}},
    {"stdout_tail": TEXT.replace(HEAD, "0" * 40)},      # the verdict names another head
    {"stdout_tail": TEXT.replace("FILES READ: src/lib.rs:3", "FILES READ: none")},
    {"pinned": True},
    {"stdout_tail": TEXT.replace(f"HEAD: {HEAD}", f"HEAD: {'0' * 40}").replace("src/lib.rs:3", f"src/lib.rs:3 at {HEAD}")},   # the HEAD line names another head
    {"stdout_tail": TEXT.replace(f"HEAD: {HEAD}\n", "").replace("src/lib.rs:3", f"src/lib.rs:3 at {HEAD}")},                     # the sha only appears in prose
    {"stdout_tail": TEXT.replace(f"HEAD: {HEAD}\n", f"```\nHEAD: {HEAD}\n```\n")},                  # only a fenced HEAD line
    {"stdout_tail": TEXT.replace(f"HEAD: {HEAD}\n", f"HEAD: {HEAD}\nHEAD: unknown\n")},            # a malformed second declaration
    {"stdout_tail": TEXT.replace(f"HEAD: {HEAD}\n", f"HEAD: {HEAD}\nHEAD: {'0' * 40}\n")},         # a conflicting second declaration
    # a fence marker with trailing text does not close the block: the HEAD line is still inside it
    {"stdout_tail": TEXT.replace(f"HEAD: {HEAD}\n", f"```\n``` still code\nHEAD: {HEAD}\n```\n")},
    {"completion": {}},                                 # no head_sha: a refusal, never a TypeError
    {"completion": {"head_sha": None}},
])
def test_an_agy_seat_without_its_recorded_facts_is_refused(tmp_path, changes):
    with pytest.raises(ValueError, match="not a passing witnessed seat"):
        seats(mirror(tmp_path, **changes), allow_unwitnessed=True)


def test_a_tests_line_inside_a_fence_the_trailing_marker_did_not_close_is_not_a_test_run(tmp_path):
    text = TEXT.replace("TESTS: pytest -q tests/test_fixture.py: 3 passed\n",
                        "```\n``` still code\nTESTS: pytest -q: 3 passed\n```\n")
    with pytest.raises(ValueError, match="not a passing witnessed seat"):
        seats(mirror(tmp_path, leg=OPENCODE, harness="opencode", text=text), allow_unwitnessed=True)


def test_a_pg_spawnd_record_names_its_harness_only_on_the_chosen_leg(tmp_path):
    """pg-spawnd writes no top-level harness: the chosen leg's harness stands for it, a mismatch still refuses."""
    (seat,) = seats(mirror(tmp_path, harness=None), allow_unwitnessed=True)
    assert seat["harness"] == "agy"


def test_a_record_harness_that_contradicts_the_chosen_leg_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a passing witnessed seat"):
        seats(mirror(tmp_path, harness="opencode"), allow_unwitnessed=True)


def test_an_unwitnessed_seat_is_refused_unless_the_caller_allows_it(tmp_path):
    with pytest.raises(ValueError, match="not a passing witnessed seat"):
        seats(mirror(tmp_path))


@pytest.mark.parametrize("tests,accepted", [
    ("TESTS: cargo test -p fixture: 4 passed", True),
    ("TESTS: none: no shell", False),
    ("TESTS: pytest -q: 0 passed", False),
    ("TESTS: cargo test: 0 tests, 0 failed", False),
    ("TESTS: pytest -q: no tests ran", False),
    ("TESTS: pytest -q: 3 passed, 0 failed", True),
    ("TESTS: pytest (requires approval)", False),
    ("TESTS: pytest --collect-only: 3 tests collected", False),   # collecting is not running
    ("TESTS: cargo test --no-run: 3 tests", False),
    ("TESTS: pytest -q: 3 selected", False),
    ("TESTS: pytest -q: 0 tests passed, 4 skipped", False),    # nothing executed
    ("TESTS: pytest -q: 0 passed, 4 skipped", False),
    ("TESTS: pytest -q: 12 tests passed", True),
    ("", False),
])
def test_an_opencode_seat_counts_only_when_tests_ran(tmp_path, tests, accepted):
    text = TEXT.replace("TESTS: pytest -q tests/test_fixture.py: 3 passed\n", f"{tests}\n" if tests else "")
    root = mirror(tmp_path, leg=OPENCODE, harness="opencode", text=text)
    if accepted:
        assert seats(root, allow_unwitnessed=True)[0]["harness"] == "opencode"
    else:
        with pytest.raises(ValueError, match="not a passing witnessed seat"):
            seats(root, allow_unwitnessed=True)


def test_an_auth_lane_keeps_refusing_an_unwitnessed_seat(unseated_w, tmp_path):
    w = unseated_w
    root = mirror(tmp_path)
    w.store.ctx["spawner_records"] = SpawnerRecordAdapter(root)
    w.decision_path.write_text(f"| D-5504 | demo | GO {HEAD}; Review-Runs: run-one |\n", encoding="utf-8")
    w.store.put_card(w.p("router"), "demo:H", {"outcome": "fixture", "risk_tags": ["auth"]})
    with pytest.raises(Refused, match="NO-WITNESS"):
        w.store.close_merged(w.p("router"), "demo:H", "D-5504", None)


def test_a_non_auth_lane_closes_on_an_unwitnessed_agy_seat(unseated_w, tmp_path):
    w = unseated_w
    root = mirror(tmp_path)  # the high tier needs a second, witnessed family beside the agy seat
    path = root / "fixture" / "run-two" / "record.json"
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({
        "run_id": "run-two", "host": "fixture", "item_key": "demo:H", "role": "review", "status": "completed",
        "exit_code": 0, "served_model": "deepseek-test", "finish_reason": "stop", "attempts": [{}],
        "completion": {"head_sha": HEAD}, "stdout_tail": "TEST_GAMING: no\nVERDICT: PASS\n"}), encoding="utf-8")
    path.chmod(0o600)
    w.store.ctx["spawner_records"] = SpawnerRecordAdapter(root)
    w.decision_path.write_text(f"| D-5504 | demo | GO {HEAD}; Review-Runs: run-one, run-two |\n", encoding="utf-8")
    assert w.store.close_merged(w.p("router"), "demo:H", "D-5504", None)["state"] == "done"
