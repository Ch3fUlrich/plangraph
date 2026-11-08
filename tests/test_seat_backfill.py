"""Hermetic seat-record discovery and backfill regression tests."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_record as sr
import seat_verdict as sv

SHA = "a" * 40
ITEM = {"key": "demo:A", "head_sha": SHA, "writer_families": ["openai"]}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def record(tmp_path, **changes):
    run = tmp_path / ("run-" + "b" * 32)
    run.mkdir()
    data = dict(run_id=run.name, role="review", item_key="demo:A",
                status="completed", exit_code=0, completion={"head_sha": SHA},
                served_model="deepseek/deepseek-v3", served_family="deepseek",
                chosen_leg={"model": "deepseek/deepseek-v3", "family": "deepseek"},
                card_hash="c" * 64, plan={}, started=1, ended=2,
                cwd="/work", attempts=[{"status": "completed"}],
                finish_reason="stop", stdout_tail="", stderr_tail="")
    data.update(changes)
    (run / "record.json").write_text(json.dumps(data))
    (run / "record.json").chmod(0o600)
    (run / "live.log").write_text("VERDICT: PASS\nTEST_GAMING: no\n")
    return run


@pytest.mark.parametrize("case,reason", [
    ("family", "family same as writer"), ("head", "head mismatch"),
    ("verdict", "no VERDICT"), ("forged", "spawner fields")])
def test_seat_backfill_refusals(tmp_path, case, reason):
    changes = {"served_model": "gpt-5"} if case == "family" else {}
    if case == "head":
        changes["completion"] = {"head_sha": "d" * 40}
    if case == "forged":
        changes["card_hash"] = None
    run = record(tmp_path, **changes)
    if case == "verdict":
        (run / "live.log").write_text("VERDICT: maybe\n")
    eligible, reasons = sr.eligible_records(ITEM, tmp_path)
    assert eligible == []
    assert reason in " ".join(reasons)


def test_seat_backfill_eligible(tmp_path):
    run = record(tmp_path)
    seats, _ = sr.eligible_records(ITEM, tmp_path)
    assert [(s["run_id"], s["family"], s["ok"]) for s in seats] == [
        (run.name, "deepseek", True)]


def test_seat_backfill_dry_run_no_network_missing(tmp_path, capsys):
    snapshot = tmp_path / "plan.snapshot.json"
    snapshot.write_text(json.dumps({"items": [dict(ITEM, state="in_review")]}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        assert sv.main(["--backfill-dry-run", "--snapshot", str(snapshot),
                        "--records-dir", str(tmp_path)]) == 0
    assert "demo:A\tMISSING no record" in capsys.readouterr().out


def test_seat_backfill_snapshot_apply_reuses_poster(tmp_path, capsys):
    from types import SimpleNamespace
    from unittest.mock import Mock
    run = record(tmp_path)
    snapshot = tmp_path / "plan.snapshot.json"
    snapshot.write_text(json.dumps({"items": [dict(
        key=ITEM["key"], state="in_review", epoch=3, writer_model="gpt-5",
        evidence=[dict(kind="commit", ref={"commit": SHA}, epoch=3, verified=True)])]}))
    args = SimpleNamespace(snapshot=snapshot, records_dir=tmp_path,
                           backfill_apply=False, token_dir=tmp_path, url="http://invalid")
    poster = Mock(return_value=0)
    with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        assert sr.backfill(args, poster) == 0
    poster.assert_not_called()
    assert f"{run.name}:deepseek:PASS" in capsys.readouterr().out
    args.backfill_apply = True
    assert sr.backfill(args, poster) == 0
    assert poster.call_count == 1
    assert poster.call_args.args[0][:2] == ["--key", ITEM["key"]]


@pytest.fixture
def snapshot_shape():
    def make(rows):
        return dict(key="demo:A", project="demo", track="lane", epoch=9,
                    writer_model=None, run_ids=[], evidence=[
                        dict(kind=k, verified=v, epoch=ep, at=at,
                             ref={"commit": sha}) for k, v, ep, at, sha in rows])
    return make


@pytest.mark.parametrize("rows,head,reason", [
    ([("commit+ci", True, 1, "a", SHA), ("commit+ci", False, 9, "b", "b"*40)], SHA, None),
    ([("commit", True, 1, "a", SHA), ("commit+ci", False, 9, "b", "b"*40)], SHA, None),
    ([("commit+ci", True, 1, "a", SHA), ("commit+ci", True, 2, "b", "b"*40)], "b"*40, None),
    ([("commit+ci", True, 2, "a", SHA), ("commit+ci", True, 2, "b", "b"*40)], None, "head mismatch"),
    ([("decision", True, 9, "a", SHA)], None, "no commit evidence"),
    ([("commit+ci", False, 9, "a", SHA)], None, "no commit evidence"),
])
def test_seat_snapshot_shapes(snapshot_shape, rows, head, reason):
    item = sr.snapshot_item(snapshot_shape(rows))
    assert item["head_sha"] == head
    if reason:
        assert reason in item["missing_reason"]


@pytest.mark.parametrize("source", ["trailer", "record"])
def test_seat_snapshot_writer_sources(tmp_path, snapshot_shape, source):
    item = snapshot_shape([("commit+ci", True, 1, "a", SHA)])
    run = record(tmp_path, role="implement", served_model="gpt-5")
    from unittest.mock import Mock
    git = Mock()
    git.repo_path.return_value = str(tmp_path)
    git.writer_trailers.return_value = ([{"Writer": "omniroute/gpt-5 run-abc"}]
                                        if source == "trailer" else [])
    if source == "trailer":
        (run / "record.json").unlink()
    normalized = sr.snapshot_item(item, records_dir=tmp_path, git=git)
    assert normalized["writer_families"] == ["openai"]


def test_seat_nested_records(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    run = record(host)
    seats, _ = sr.eligible_records(ITEM, tmp_path)
    assert seats[0]["run_id"] == run.name


def test_seat_trailer_real_git(tmp_path, snapshot_shape):
    import subprocess
    from plangraph.keeper.evidence_git import GitAdapter
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit",
        "--allow-empty", "-qm", "fixture\n\nWriter: omniroute/gpt-5 run-fixture")
    sha = git("rev-parse", "HEAD")
    item = snapshot_shape([("commit+ci", True, 1, "a", sha)])
    primary = tmp_path / "primary"
    primary.mkdir()
    git("init", "-q", str(primary))
    adapter = GitAdapter({"demo": str(primary), "demo/lane": str(tmp_path)})
    adapter._maybe_fetch = lambda repo: False
    assert sr.snapshot_item(item, git=adapter)["writer_families"] == ["openai"]


def test_seat_backfill_reason_tally(tmp_path, snapshot_shape, capsys):
    item = snapshot_shape([("decision", True, 9, "a", SHA)])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"items": [dict(item, state="in_review")]}))
    assert sv.main(["--backfill-dry-run", "--snapshot", str(snapshot),
                    "--records-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "demo:A\tMISSING no commit evidence" in output
    assert "counts\titems=1\teligible=0\tmissing=1\tfailed=0" in output
    assert "reason\tno commit evidence\t1" in output
