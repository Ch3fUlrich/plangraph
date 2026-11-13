"""Round-two item binding, attribution, outbox isolation and library guards."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_seat_backfill import ITEM, SHA, record, test_seat_trailer_real_git as git_test
from test_seat_backfill_replay import backfill_case
import seat_record as sr
import seat_verdict as sv


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("binding", ["key", "prompt", "pr"])
def test_reasons_are_only_for_bound_records(tmp_path, bound, binding):
    unrelated = record(tmp_path, item_key="demo:B", status="failed")
    (unrelated / "prompt.txt").write_text("Review item demo:B PR #8")
    if bound:
        host = tmp_path / "bound"
        host.mkdir()
        run = record(host, completion={}, item_key="demo:A" if binding == "key" else None)
        (run / "prompt.txt").write_text("demo:A" if binding == "prompt" else "Review PR #7")
    seats, reasons = sr.eligible_records(dict(ITEM, pr_number=7), tmp_path)
    assert seats == []
    assert len(reasons) == 1
    assert "item key mismatch" not in reasons[0]
    assert ("record completion.head_sha missing" if bound else "no record [seat=review") in reasons[0]
    if not bound:
        assert "item=demo:A PR=7" in reasons[0]


def test_absent_record_tally_ignores_other_item(backfill_case, capsys):
    args, run = backfill_case
    path = run / "record.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), item_key="demo:B")))
    args.backfill_apply, args.format = False, "tsv"
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    output = capsys.readouterr().out
    assert "demo:A\tMISSING no record" in output
    assert "reason\tno record\t1" in output
    assert "item key mismatch" not in output
    poster.assert_not_called()


@pytest.mark.parametrize("model,expected", [("deepseek/deepseek-v3", ["deepseek", "openai"]),
                                            ("unknown-model", [])])
def test_writer_sources_are_unioned_and_unknowns_refuse(tmp_path, model, expected):
    record(tmp_path, role="implement", served_model=model)
    git = Mock()
    git.repo_path.return_value = str(tmp_path)
    git.writer_trailers.return_value = [{"Writer": "openai/gpt-5 run-fixture"}]
    writers = sr.writer_families(ITEM, SHA, tmp_path, git)
    assert writers == expected
    review = tmp_path / "review"
    review.mkdir()
    record(review)
    seats, reasons = sr.eligible_records(dict(ITEM, writer_families=writers), tmp_path)
    assert seats == []
    assert ("family same as writer" if expected else "unknown writer") in " ".join(reasons)


@pytest.mark.parametrize("location", ["record", "plan", "chosen_leg"])
@pytest.mark.parametrize("key", ["pin", "pinned", "spawn_pin"])
@pytest.mark.parametrize("value", [0, "", None, [], False])
def test_pin_presence_is_fail_closed(tmp_path, location, key, value):
    changes = {key: value} if location == "record" else {
        location: dict({"model": "deepseek/deepseek-v3", "family": "deepseek"}
                       if location == "chosen_leg" else {}, **{key: value})}
    run = record(tmp_path, **changes)
    if value is False:
        assert sr.evidence_from_record(run)[0]["head_sha"] == SHA
        assert len(sr.eligible_records(ITEM, tmp_path)[0]) == 1
    else:
        with pytest.raises(ValueError, match="pinned seat"):
            sr.evidence_from_record(run)
        seats, reasons = sr.eligible_records(ITEM, tmp_path)
        assert seats == []
        assert "pinned seat" in " ".join(reasons)


@pytest.mark.parametrize("eligible", [False, True])
def test_discovery_json_has_delivery_fields(backfill_case, capsys, eligible):
    args, run = backfill_case
    args.backfill_apply = False
    if not eligible:
        (run / "live.log").write_text("no verdict\n")
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == ("ELIGIBLE" if eligible else "MISSING")
    assert row["delivery"] is None
    assert row["verifier_exit"] is None
    poster.assert_not_called()


def test_symlink_partition_skips_are_explicit_without_traversal(tmp_path, monkeypatch):
    outside, root = tmp_path / "outside", tmp_path / "records"
    outside.mkdir()
    root.mkdir()
    record(outside)
    (root / "alias").symlink_to(outside, target_is_directory=True)
    original = Path.read_text
    def read(path, *a, **kw):
        assert "alias" not in path.parts, "must not read through a skipped partition"
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, "read_text", read)
    index = sr.record_index(root)
    assert dict(index) == {}
    assert index.skips == ["symlinked directory skipped: alias"]
    assert sr.eligible_records(ITEM, index)[1][0].startswith("no record [seat=review")


def test_backfill_reports_skipped_partition_separately(backfill_case, capsys):
    args, run = backfill_case
    args.backfill_apply = False
    (args.records_dir / "alias").symlink_to(run, target_is_directory=True)
    assert sr.backfill(args, Mock()) == 0
    output = capsys.readouterr()
    row = json.loads(output.out)[0]
    assert row["status"] == "ELIGIBLE"
    assert row["reasons"] == []
    assert "symlinked directory skipped: alias" in output.err


@pytest.mark.parametrize("guard", ["dry_run", "backfill_dry_run", "missing_apply"])
def test_library_entry_never_posts_without_unambiguous_apply(backfill_case, capsys, guard):
    args, run = backfill_case
    if guard == "missing_apply":
        del args.backfill_apply
    else:
        setattr(args, guard, True)
    before = {p: p.read_bytes() for p in run.iterdir()}
    poster = Mock(return_value=0)
    assert sr.backfill(args, poster) == (0 if guard == "missing_apply" else 1)
    if guard != "missing_apply":
        assert "without --dry-run or --backfill-dry-run" in capsys.readouterr().err
    poster.assert_not_called()
    assert before == {p: p.read_bytes() for p in run.iterdir()}


@pytest.mark.parametrize("apply,dry", [(False, False), (True, True)])
def test_apply_helper_cannot_bypass_explicit_flag(backfill_case, apply, dry):
    args, run = backfill_case
    args.backfill_apply, args.dry_run = apply, dry
    seat = sr.eligible_records(ITEM, args.records_dir)[0][0]
    row, poster = {"reasons": []}, Mock()
    assert sv.apply_backfill(args, ITEM, seat, poster, row) == 1
    assert row["delivery"] == "failed"
    assert row["reasons"]
    poster.assert_not_called()
    assert not (run / "verdict.json.lock").exists()


@pytest.mark.parametrize("rc", [None, False, "0", -1])
def test_invalid_poster_result_is_ambiguous_and_never_replayed(backfill_case, capsys, rc):
    args, run = backfill_case
    poster = Mock(return_value=rc)
    assert sr.backfill(args, poster) == 1
    row = json.loads(capsys.readouterr().out)[0]
    assert row["delivery"] == "failed"
    assert "verifier result" in " ".join(row["reasons"])
    assert row["verifier_exit"] is None
    assert json.loads((run / "verdict.json").read_text())["status"] == "sending"
    assert sr.backfill(args, poster) == 0
    poster.assert_called_once()


@pytest.mark.parametrize("failure", ["outbox-check", "open", "fdopen", "lock", "journal-read",
                                    "sending-write", "poster", "outcome-write", "marker-write"])
@pytest.mark.parametrize("format", ["json", "tsv"])
def test_one_outbox_failure_does_not_block_or_corrupt_others(backfill_case, monkeypatch,
                                                           capsys, failure, format):
    from plangraph.spawner import notify, private_io
    args, run = backfill_case
    args.format = format
    (run / "live.log").write_text("VERDICT: CHANGES\nTEST_GAMING: no\n")
    accepting_host = args.records_dir / "accepting"
    accepting_host.mkdir()
    accepting = record(accepting_host)
    host, writer = args.records_dir / "other", args.records_dir / "writer-b"
    host.mkdir()
    writer.mkdir()
    healthy = record(host, item_key="demo:B")
    record(writer, role="implement", item_key="demo:B", served_model="gpt-5")
    snapshot = json.loads(args.snapshot.read_text())
    snapshot["items"].append(dict(snapshot["items"][0], key="demo:B"))
    args.snapshot.write_text(json.dumps(snapshot))
    opened, written, read_text = os.open, private_io.atomic_write_private, Path.read_text
    fdopen, is_symlink = os.fdopen, Path.is_symlink
    def check(path):
        if failure == "outbox-check" and path == run / "verdict.json":
            raise PermissionError("fixture")
        return is_symlink(path)
    def open_fd(fd, *a, **kw):
        if failure == "fdopen" and os.fstat(fd).st_ino == (run / "verdict.json.lock").stat().st_ino:
            os.close(fd)
            raise OSError("fixture")
        return fdopen(fd, *a, **kw)
    if failure == "journal-read":
        (run / "verdict.json").write_text('{"status": "retry", "attempts": 1}')
    def read(path, *a, **kw):
        if failure == "journal-read" and path == run / "verdict.json":
            raise PermissionError("fixture")
        return read_text(path, *a, **kw)
    def open_file(path, *a, **kw):
        if failure == "open" and str(path) == str(run / "verdict.json.lock"):
            raise PermissionError("fixture")
        return opened(path, *a, **kw)
    def write(path, text):
        if failure == "marker-write" and path == run / "verdict.delivered":
            raise PermissionError("fixture")
        status = json.loads(text)["status"] if path.name == "verdict.json" else None
        if path == run / "verdict.json" and (
                failure == "sending-write" or failure == "outcome-write" and status == "delivered"):
            raise OSError("fixture")
        return written(path, text)
    if failure == "lock":
        if os.name == "nt":
            pytest.skip("POSIX flock error injection")
        import fcntl
        flock = fcntl.flock
        identity = (run / "verdict.json.lock")
        def lock(fd, operation):
            if os.fstat(fd.fileno()).st_ino == identity.stat().st_ino:
                raise OSError("fixture lock error")
            return flock(fd, operation)
        monkeypatch.setattr(fcntl, "flock", lock)
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fdopen", open_fd)
    monkeypatch.setattr(Path, "is_symlink", check)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(private_io, "atomic_write_private", write)
    monkeypatch.setattr(notify, "atomic_write_private", write)
    calls = []
    def post(argv):
        calls.append(argv[1])
        if failure == "poster" and argv[1] == "demo:A":
            raise RuntimeError("fixture unexpected crash")
        return 0
    assert sr.backfill(args, post) == 1
    output = capsys.readouterr().out
    assert "fixture" not in output
    if format == "json":
        rows = json.loads(output)
        assert len(rows) == 2
        assert rows[0]["delivery"] == "failed"
        assert rows[0]["reasons"]
        assert rows[1]["delivery"] == "delivered"
        assert rows[1]["reasons"] == []
    else:
        assert "demo:A\tELIGIBLE" in output and "delivery=failed" in output
        assert "demo:B\tELIGIBLE" in output and "delivery=delivered" in output
        assert "failed=1" in output
    assert (healthy / "verdict.delivered").exists()
    assert json.loads((healthy / "verdict.json").read_text())["status"] == "delivered"
    assert not (accepting / "verdict.json").exists()
    if failure in ("poster", "outcome-write", "marker-write"):
        assert json.loads((run / "verdict.json").read_text())["status"] == (
            "delivered" if failure == "marker-write" else "sending")
        assert sr.backfill(args, post) == 0
        assert calls == ["demo:A", "demo:B"]
    else:
        assert calls == ["demo:B"]
        if failure == "journal-read":
            assert json.loads(read_text(run / "verdict.json"))["status"] == "retry"
        else:
            assert not (run / "verdict.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shared forward flock")
def test_notify_real_lock_blocks_backfill_then_forward_posts_once(backfill_case, monkeypatch):
    from plangraph.spawner import notify
    args, run = backfill_case
    data = dict(json.loads((run / "record.json").read_text()), given_writer_families=["openai"])
    keeper = SimpleNamespace(url=args.url, item=lambda _: {"key": "demo:A", "evidence": [
        {"kind": "commit+ci", "verified": True, "ref": {"commit": SHA}}]})
    monkeypatch.setenv("PG_REVIEWER_TOKEN_DIR", str(args.token_dir))
    sender = Mock(return_value=(200, "ok"))
    monkeypatch.setattr(sv, "send", sender)
    real_lock, paths = notify.inbox_lock, []
    from contextlib import contextmanager
    @contextmanager
    def hold_forward_lock(path):
        paths.append(path)
        with real_lock(path):
            assert sr.backfill(args, sv.main) == 0
            sender.assert_not_called()
            assert not (run / "verdict.json").exists()
            yield
    monkeypatch.setattr(notify, "inbox_lock", hold_forward_lock)
    notify.deliver_verdict(run, data, keeper)
    assert paths == [run / "verdict.json"]
    assert sr.backfill(args, sv.main) == 0
    sender.assert_called_once()


def test_git_fixture_has_missing_executable_skip():
    marks = [mark for mark in getattr(git_test, "pytestmark", []) if mark.name == "skipif"]
    assert marks
    assert any("git" in mark.kwargs.get("reason", "").lower() for mark in marks)
