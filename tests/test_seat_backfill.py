"""Hermetic seat-record discovery and backfill regression tests."""
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_record as sr
import seat_verdict as sv

SHA = "a" * 40
ITEM = {"key": "demo:A", "head_sha": SHA, "writer_families": ["openai"]}


@pytest.mark.parametrize("case,accepted", [
    ("prompt", True), ("pr", False), ("head", False),
    ("family", False), ("comment", False)])


def test_seat_prompt_link(tmp_path, case, accepted):
    changes = {"item_key": None}
    if case == "head":
        changes["completion"] = {"head_sha": "d" * 40}
    if case == "family":
        changes["served_model"] = "gpt-5"
    run = record(tmp_path, **changes)
    prompt = "Review PR #8, item demo:A" if case == "pr" else "Review PR #7, item demo:A"
    if case != "comment":
        (run / "prompt.txt").write_text(prompt)
    item = dict(ITEM, pr_number=7, comments="PR #7 VERDICT: PASS")
    seats, reasons = sr.eligible_records(item, tmp_path)
    assert bool(seats) is accepted, reasons


def test_seat_index_reads_records_once(tmp_path, monkeypatch):
    run = record(tmp_path)
    original = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path.name == "record.json":
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    index = sr.record_index(tmp_path)
    for _ in range(3):
        assert sr.eligible_records(ITEM, index)[0]
    assert reads == [run / "record.json"]


def test_seat_backfill_cached_pr_json(tmp_path, capsys, snapshot_shape):
    run = record(tmp_path, item_key=None)
    (run / "prompt.txt").write_text("Review PR #7")
    snapshot = tmp_path / "snapshot.json"
    writer = tmp_path / "writer"
    writer.mkdir()
    record(writer, role="implement", served_model="gpt-5")
    item = snapshot_shape([("commit", True, 1, None, SHA)])
    snapshot.write_text(json.dumps({"items": [dict(item, state="in_review")]}))
    cache = tmp_path / "prs.json"
    cache.write_text(json.dumps({SHA: 7}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        assert sv.main(["--backfill-dry-run", "--snapshot", str(snapshot),
                        "--records-dir", str(tmp_path), "--pr-cache", str(cache),
                        "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["status"] == "ELIGIBLE"
    assert rows[0]["pr_number"] == 7


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


def test_seat_backfill_dry_run_no_network_missing(tmp_path, capsys, snapshot_shape):
    snapshot = tmp_path / "plan.snapshot.json"
    record(tmp_path, role="implement", served_model="gpt-5")
    item = snapshot_shape([("commit", True, 1, None, SHA)])
    snapshot.write_text(json.dumps({"items": [dict(item, state="in_review")]}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        assert sv.main(["--backfill-dry-run", "--snapshot", str(snapshot),
                        "--records-dir", str(tmp_path)]) == 0
    assert "demo:A\tMISSING no record" in capsys.readouterr().out


def test_seat_backfill_snapshot_apply_reuses_poster(tmp_path, capsys):
    from types import SimpleNamespace
    from unittest.mock import Mock
    run = record(tmp_path)
    writer = tmp_path / "writer"
    writer.mkdir()
    record(writer, role="implement", served_model="gpt-5")
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
    git.writer_trailers.return_value = ([{"Writer": "openai/gpt-5 run-abc"}]
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


@pytest.mark.skipif(shutil.which("git") is None, reason="git executable unavailable")
def test_seat_trailer_real_git(tmp_path, snapshot_shape):
    import subprocess
    from plangraph.keeper.evidence_git import GitAdapter
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True, timeout=10).strip()
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit",
        "--allow-empty", "-qm", "fixture\n\nWriter: openai/gpt-5 run-fixture")
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


def test_snapshot_writer_model_is_not_evidence():
    assert sr.writer_families(dict(ITEM, writer_model="gpt-5"), SHA, None, None) == []


@pytest.mark.parametrize("at", [None, "same", 10])
def test_newest_evidence_epoch_breaks_timestamp_ties(snapshot_shape, at):
    item = snapshot_shape([("commit", True, 2, at, SHA),
                           ("commit", True, 10, at, "b" * 40)])
    assert sr.snapshot_item(item)["head_sha"] == "b" * 40


def test_numeric_evidence_times_are_not_lexicographic(snapshot_shape):
    item = snapshot_shape([("commit", True, 1, 9, SHA),
                           ("commit", True, 2, 10, "b" * 40)])
    assert sr.snapshot_item(item)["head_sha"] == "b" * 40


def test_raw_derived_fields_cannot_bypass_ambiguity(tmp_path, capsys):
    from types import SimpleNamespace
    from unittest.mock import Mock
    item = dict(ITEM, state="in_review", evidence=[
        dict(kind="commit", verified=True, epoch=2, ref=sha)
        for sha in (SHA, "b" * 40)])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"items": [item]}))
    args = SimpleNamespace(snapshot=snapshot, records_dir=tmp_path, backfill_apply=True)
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    assert "ambiguous verified commits" in capsys.readouterr().out
    poster.assert_not_called()


@pytest.mark.parametrize("returned_missing", [False, True])
def test_missing_completion_head_is_rejection(tmp_path, returned_missing):
    record(tmp_path, completion={})
    if returned_missing:
        with patch.object(sr, "evidence_from_record", return_value=({}, "VERDICT: PASS")):
            seats, reasons = sr.eligible_records(ITEM, tmp_path)
    else:
        seats, reasons = sr.eligible_records(ITEM, tmp_path)
    assert not seats
    assert "head" in " ".join(reasons)


@pytest.mark.parametrize("finish,accepted", [("stop", True), ("end_turn", True),
                                             ("END_TURN", False), ("length", False)])


def test_discovery_preserves_poster_finish_set(tmp_path, finish, accepted):
    record(tmp_path, finish_reason=finish)
    assert bool(sr.eligible_records(ITEM, tmp_path)[0]) is accepted


def test_symlinked_host_partition_rejected(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    run = record(host)
    alias = tmp_path / "alias"
    alias.symlink_to(host, target_is_directory=True)
    path = alias / run.name / "record.json"
    index = {path: (json.loads(path.read_text()), "")}
    seats, reasons = sr.eligible_records(ITEM, index)
    assert not seats
    assert "spawner fields" in " ".join(reasons)


def test_symlinked_writer_partition_is_not_evidence(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    run = record(host, role="implement", served_model="gpt-5")
    alias = tmp_path / "alias"
    alias.symlink_to(host, target_is_directory=True)
    path = alias / run.name / "record.json"
    index = {path: (json.loads(path.read_text()), "")}
    assert sr.writer_families(ITEM, SHA, index, None) == []


@pytest.mark.parametrize("completion", [{}, "malformed"])
def test_missing_head_backfill_tallied(tmp_path, snapshot_shape, capsys, completion):
    record(tmp_path, completion=completion)
    host = tmp_path / "writer"
    host.mkdir()
    record(host, role="implement", served_model="gpt-5")
    snapshot = tmp_path / "snapshot.json"
    item = dict(snapshot_shape([("commit", True, 1, None, SHA)]), state="in_review")
    snapshot.write_text(json.dumps({"items": [item]}))
    assert sv.main(["--backfill-dry-run", "--snapshot", str(snapshot),
                    "--records-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "reason\trecord completion.head_sha missing or not 40 hex chars\t1" in output


@pytest.mark.parametrize("case", ["head", "record-key", "prompt-key", "reason", "namespace"])
def test_round_two_binding(tmp_path, case):
    run = record(tmp_path, **({"item_key": "demo:B"} if case == "record-key" else {}))
    (run / "prompt.txt").write_text("Review PR #7 item " + ("demo:B" if case == "prompt-key" else "demo:A"))
    if case == "namespace":
        assert sv.model_family("unknown-provider/unknown-model") is None
        with patch.object(sr, "evidence_from_record", return_value=({}, "VERDICT: FIX")):
            assert not sr.eligible_records(ITEM, tmp_path)[0]
        return
    evidence = {"head_sha": "d" * 40} if case == "head" else {}
    if case in ("head", "reason"):
        with patch.object(sr, "evidence_from_record", return_value=(evidence, "VERDICT: FIX")):
            seats, reasons = sr.eligible_records(dict(ITEM, pr_number=7), tmp_path)
    else:
        seats, reasons = sr.eligible_records(dict(ITEM, pr_number=7), tmp_path)
    assert not seats
    if case in ("record-key", "prompt-key"):
        assert "item key mismatch" in " ".join(reasons)
    if case == "reason":
        assert "missing or not 40 hex chars" in " ".join(reasons)
    if case == "head":
        assert "head mismatch" in " ".join(reasons)


@pytest.mark.parametrize("case", ["dry-gh", "bad-cache", "missing-cache", "timeout", "string-pr", "missing-key"])
def test_round_two_backfill(tmp_path, capsys, snapshot_shape, case):
    import subprocess
    from types import SimpleNamespace
    record(tmp_path, item_key=None)
    run = next(tmp_path.glob("run-*"))
    (run / "prompt.txt").write_text("Review PR #7")
    writer = tmp_path / "writer"
    writer.mkdir()
    record(writer, role="implement", served_model="gpt-5")
    item = dict(snapshot_shape([("commit", True, 1, None, SHA)]), state="in_review")
    if case == "missing-key":
        del item["key"]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"items": [item, dict(item, key="demo:C")]}))
    cache = tmp_path / "cache.json"
    cache.write_text("{" if case == "bad-cache" else json.dumps({SHA: "7"}))
    if case == "missing-cache":
        cache.unlink()
    args = SimpleNamespace(snapshot=snapshot, records_dir=tmp_path, backfill_apply=False,
                           backfill_dry_run=True, pr_lookup_gh=case == "dry-gh",
                           pr_cache=cache if "cache" in case or case == "string-pr" else None,
                           format="json")
    lookup = (lambda sha: (_ for _ in ()).throw(subprocess.TimeoutExpired("fixture", 1))) if case == "timeout" else None
    with patch("subprocess.run", side_effect=AssertionError("network")):
        assert sr.backfill(args, lambda _: pytest.fail("poster"), lookup) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert rows[1]["status"] == "MISSING"
    assert rows[1]["pr_number"] == (7 if case == "string-pr" else None)
    if case == "string-pr":
        assert rows[0]["status"] == "ELIGIBLE"
        assert rows[0]["pr_number"] == 7
        assert rows[0]["reasons"] == []
    else:
        assert rows[0]["status"] == "MISSING"
        assert rows[0]["pr_number"] is None
        expected = "snapshot item missing key" if case == "missing-key" else "no record"
        assert expected in " ".join(rows[0]["reasons"])
