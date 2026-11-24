"""A red Actions run is imported into the landing timeline before the refusal, and a listed item is found."""
import pytest


@pytest.fixture
def land(tools_importable):
    import merge_batch_land
    return merge_batch_land


def red_run(head, **over):
    run = {"id": 7, "head_sha": head, "head_branch": "batch/x", "event": "pull_request",
           "status": "completed", "conclusion": "failure", "run_attempt": 1}
    run.update(over)
    return run


def test_a_red_run_is_imported_before_the_refusal(land, monkeypatch):
    head = "a" * 40
    imported = []
    monkeypatch.setattr(land, "gh_json", lambda *args: {"workflow_runs": [red_run(head)]})
    monkeypatch.setattr(land, "run_timing_import", lambda *args: imported.append(args))
    with pytest.raises(land.MergeBatchError, match="not green"):
        land.require_full_run("owner/proj", head, "batch/x", "train-1")
    assert [(a[1], a[2], a[3]["id"], a[4]) for a in imported] == [("train-1", head, 7, 1)]


def test_a_run_on_another_head_or_still_running_is_not_imported(land, monkeypatch):
    head = "a" * 40
    imported = []
    monkeypatch.setattr(land, "run_timing_import", lambda *args: imported.append(args))
    for run in (red_run("b" * 40), red_run(head, status="in_progress", conclusion=None)):
        monkeypatch.setattr(land, "gh_json", lambda *args, run=run: {"workflow_runs": [run]})
        with pytest.raises(land.MergeBatchError):
            land.require_full_run("owner/proj", head, "batch/x", "train-1")
    assert imported == []


def test_listed_items_are_only_those_of_the_train(tools_importable):
    import merge_batch_build as build
    report = "closed\tk:A\tabc\nlisted\tk:B\topen\tseats\nlisted\tk:C\topen\tx\nlisted\tk:B\topen\ty\n"
    assert build.listed_items(report, {"k:A", "k:B"}) == ["k:B"]
    assert build.listed_items("", {"k:A"}) == []
