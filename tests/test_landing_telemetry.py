"""LAND-MODE-MEASURE: one telemetry row per landing and the report that compares the modes.

Mutations killed: a landing without a row accepted, a row without its mode, a duplicate row for one
landing, a median or worst taken over the wrong column, a mode missing from the report, a minute
count that ignores the CI window, a settle that marks a red main green.
"""
import json
import sqlite3

import pytest


@pytest.fixture
def util(tools_importable, tmp_path, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_LANDING_DB", str(tmp_path / "landing.sqlite3"))
    import merge_batch_util
    return merge_batch_util


def row(**over):
    base = {"repo": "owner/proj", "mode": "tested-ff", "train": "t1", "pr": 100, "parts": [1, 2, 3],
            "landed_sha": "a" * 40, "first_ready_at": "2026-11-21T09:00:00Z",
            "build_started_at": "2026-11-21T09:40:00Z", "ci_run_id": 7, "ci_started_at": "2026-11-21T09:50:00Z",
            "ci_ended_at": "2026-11-21T10:01:00Z", "go_at": "2026-11-21T10:02:00Z",
            "landed_at": "2026-11-21T10:03:00Z", "green_at": "2026-11-21T10:01:00Z", "ci_reruns": 0,
            "fit_commits": 1, "left_out": 0}
    base.update(over)
    return base


def test_a_recorded_landing_can_be_read_back_with_its_mode(util):
    util.record_landing(row())
    rows = util.landing_rows()
    assert [(r["repo"], r["mode"], r["train"], r["parts"]) for r in rows] == [("owner/proj", "tested-ff", "t1", [1, 2, 3])]


def test_the_same_landing_twice_is_one_row(util):
    util.record_landing(row())
    util.record_landing(row(ci_reruns=2))
    assert len(util.landing_rows()) == 1 and util.landing_rows()[0]["ci_reruns"] == 2


def test_a_landing_is_identified_by_repo_and_sha_not_by_the_train_key(util):
    util.record_landing(row(train="l1", ci_reruns=1, mode="train-pr-merge"))
    util.record_landing(row(train="pr100", mode="train-pr-merge", first_ready_at=None, ci_reruns=None, go_at="2026-11-21T10:02:30Z"))
    (only,) = util.landing_rows()  # the backfill of an already recorded landing adds, never blanks or doubles it
    assert (only["train"], only["ci_reruns"], only["first_ready_at"], only["go_at"]) == (
        "pr100", 1, "2026-11-21T09:00:00Z", "2026-11-21T10:02:30Z")


def test_an_unknown_mode_is_not_recorded(util):
    with pytest.raises(util.MergeBatchError, match="mode"):
        util.record_landing(row(mode="merge"))
    assert util.landing_rows() == []


def test_a_landing_without_its_row_fails_the_check(util):
    with pytest.raises(util.MergeBatchError, match="no telemetry row"):
        util.require_landing_row("owner/proj", "t1", "a" * 40, "tested-ff")
    util.record_landing(row())
    util.require_landing_row("owner/proj", "t1", "a" * 40, "tested-ff")
    with pytest.raises(util.MergeBatchError, match="no telemetry row"):
        util.require_landing_row("owner/proj", "t1", "a" * 40, "train-pr-merge")


def test_the_report_compares_the_modes_with_median_and_worst(util):
    util.record_landing(row(train="a", mode="train-pr-merge", landed_sha="1" * 40, landed_at="2026-11-21T10:12:00Z",
                            green_at="2026-11-21T10:23:00Z"))
    util.record_landing(row(train="b", mode="train-pr-merge", landed_sha="2" * 40, landed_at="2026-11-21T10:30:00Z",
                            green_at="2026-11-21T10:41:00Z", red_main=1, fix_tasks=2))
    util.record_landing(row(train="c"))
    report = {r["mode"]: r for r in util.landing_report()}
    assert set(report) == {"tested-ff", "train-pr-merge"}
    old, new = report["train-pr-merge"], report["tested-ff"]
    assert old["trains"] == 2 and new["trains"] == 1
    assert old["ready_to_landed_min"] == {"median": 81.0, "worst": 90.0}  # 09:00 -> 10:12 and 10:30
    assert new["ready_to_landed_min"] == {"median": 63.0, "worst": 63.0}
    assert old["landed_to_green_min"] == {"median": 11.0, "worst": 11.0}
    assert new["landed_to_green_min"] == {"median": -2.0, "worst": -2.0}
    assert old["test_min_per_part"] == pytest.approx(11 / 3)
    assert (old["red_main"], old["fix_tasks_per_train"]) == (1, 2.0)
    assert (new["red_main"], new["fix_tasks_per_train"]) == (0, None)


def test_unknown_values_stay_out_of_the_statistics(util):
    util.record_landing(row(first_ready_at=None, green_at=None))
    only = util.landing_report()[0]
    assert only["ready_to_landed_min"] is None and only["landed_to_green_min"] is None


def test_the_report_text_names_every_column(util):
    util.record_landing(row())
    text = util.render_landing_report(util.landing_report())
    for column in ("mode", "trains", "ready->landed", "landed->green", "test min/part", "red main", "fix/train"):
        assert column in text
    assert "tested-ff" in text


def test_the_database_is_a_plain_sqlite_file(util, tmp_path):
    util.record_landing(row())
    with sqlite3.connect(tmp_path / "landing.sqlite3") as db:
        assert db.execute("select mode, parts from landing").fetchall() == [("tested-ff", json.dumps([1, 2, 3]))]


def test_a_settle_reads_green_and_red_from_the_runs_on_the_landed_sha(util):
    util.record_landing(row(train="a", mode="train-pr-merge", landed_sha="1" * 40, green_at=None))
    util.record_landing(row(train="b", mode="train-pr-merge", landed_sha="2" * 40, green_at=None))
    util.record_landing(row(train="c", mode="train-pr-merge", landed_sha="3" * 40, green_at=None))
    runs = {"1" * 40: [{"status": "completed", "conclusion": "success", "updated_at": "2026-11-21T10:23:00Z"}],
            "2" * 40: [{"status": "completed", "conclusion": "failure", "updated_at": "2026-11-21T10:23:00Z"}],
            "3" * 40: [{"status": "in_progress", "conclusion": None, "updated_at": "2026-11-21T10:23:00Z"}]}
    done = util.settle_landings(lambda repo, sha: runs[sha])
    by_train = {r["train"]: r for r in util.landing_rows()}
    assert done == 2
    assert (by_train["a"]["green_at"], by_train["a"]["red_main"]) == ("2026-11-21T10:23:00Z", 0)
    assert (by_train["b"]["green_at"], by_train["b"]["red_main"]) == (None, 1)
    assert (by_train["c"]["green_at"], by_train["c"]["red_main"]) == (None, None)
