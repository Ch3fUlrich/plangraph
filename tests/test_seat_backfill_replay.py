"""Backfill shares Slice A's parser and locked delivery outbox."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_seat_backfill import SHA, record
import seat_record as sr
import seat_verdict as sv


@pytest.fixture
def backfill_case(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    run = record(tmp_path)
    writer = tmp_path / "writer"
    writer.mkdir()
    record(writer, role="implement", served_model="gpt-5")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"items": [{
        "key": "demo:A", "state": "in_review", "evidence": [{
            "kind": "commit+ci", "verified": True, "epoch": 1,
            "ref": {"commit": SHA}}]}]}))
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "reviewer-deepseek.token").write_text("dummy\n")
    args = SimpleNamespace(snapshot=snapshot, records_dir=tmp_path,
                           backfill_apply=True, token_dir=tokens,
                           url="http://fake.invalid", format="json")
    return args, run


@pytest.mark.parametrize("mode", [[], ["--backfill-dry-run"]])
def test_default_backfill_is_read_only(backfill_case, monkeypatch, capsys, mode):
    args, run = backfill_case
    before = {p: p.read_bytes() for p in args.records_dir.rglob("*") if p.is_file()}
    monkeypatch.setattr(sv, "send", lambda *a: pytest.fail("must not post"))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("subprocess"))
    monkeypatch.setattr(sr, "merged_pr", lambda *a: None)
    assert sv.main(mode + ["--snapshot", str(args.snapshot), "--records-dir",
                          str(args.records_dir), "--format", "json",
                          "--url", args.url, "--token-dir", str(args.token_dir),
                          "--pr-lookup-gh"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "ELIGIBLE"
    assert before == {p: p.read_bytes() for p in args.records_dir.rglob("*") if p.is_file()}
    assert not (run / "verdict.json.lock").exists()


@pytest.mark.parametrize("extra", [[], ["--url", "http://fake.invalid"],
                                   ["--token-dir", "tokens"],
                                   ["--url", "http://fake.invalid", "--token-dir", "tokens", "--dry-run"]])
def test_apply_requires_explicit_transport(extra, monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    assert "PG_URL" not in os.environ
    with pytest.raises(SystemExit) as exc:
        sv.main(["--backfill-apply", *extra])
    assert exc.value.code == 2


def test_cli_uses_its_own_checkout_without_install(backfill_case):
    args, _ = backfill_case
    snapshot = args.snapshot.parent / "empty.json"
    snapshot.write_text('{"items": []}')
    env = dict(os.environ, PYTHONPATH="", HOME=str(args.snapshot.parent))
    result = subprocess.run([sys.executable, str(Path(sv.__file__).resolve()),
                             "--snapshot", str(snapshot), "--records-dir",
                             str(args.records_dir), "--format", "json"],
                            cwd=args.snapshot.parent, env=env, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize("decision,ok", [("CHANGES", False), ("ACCEPT", True)])
@pytest.mark.parametrize("separator", [": ", " "])
def test_apply_keeps_main_verdict_and_replay_rules(backfill_case, monkeypatch, capsys,
                                                 decision, ok, separator):
    args, run = backfill_case
    (run / "live.log").write_text(f"VERDICT{separator}{decision}\nTEST_GAMING: no\n")
    sender = Mock(return_value=(200, "ok"))
    monkeypatch.setattr(sv, "send", sender)
    cli = ["--backfill-apply", "--snapshot", str(args.snapshot), "--records-dir",
           str(args.records_dir), "--format", "json", "--url", args.url,
           "--token-dir", str(args.token_dir)]
    for _ in range(2):
        assert sv.main(cli) == 0
        assert json.loads(capsys.readouterr().out)[0]["seats"][0]["ok"] is ok
    sender.assert_called_once()
    assert sender.call_args.args[1] == "dummy"
    assert sender.call_args.args[2]["ok"] is ok
    assert (" PASS" if ok else " FIX") in sender.call_args.args[2]["reason"]
    assert json.loads((run / "verdict.json").read_text())["status"] == "delivered"


@pytest.mark.parametrize("status", ["sending", "delivered", "skipped", "marker"])
def test_forward_outbox_prevents_backfill_replay(backfill_case, status):
    args, run = backfill_case
    path = run / ("verdict.delivered" if status == "marker" else "verdict.json")
    path.write_text("delivered\n" if status == "marker" else json.dumps({"status": status}))
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    poster.assert_not_called()


@pytest.mark.parametrize("result", [sv.EXIT_IO, sv.EXIT_REFUSED, "interrupted"])
def test_ambiguous_or_failed_apply_is_not_reposted(backfill_case, result):
    args, run = backfill_case
    poster = Mock(side_effect=RuntimeError("interrupted")) if result == "interrupted" else Mock(return_value=result)
    if result == "interrupted":
        with pytest.raises(RuntimeError, match="interrupted"):
            sr.backfill(args, poster)
    else:
        assert sr.backfill(args, poster) == 1
    assert sr.backfill(args, poster) == 0
    poster.assert_called_once()
    assert json.loads((run / "verdict.json").read_text())["status"] in ("sending", "skipped")


def test_duplicate_snapshot_rows_post_once(backfill_case):
    args, _ = backfill_case
    snapshot = json.loads(args.snapshot.read_text())
    snapshot["items"] *= 2
    args.snapshot.write_text(json.dumps(snapshot))
    poster = Mock(return_value=0)
    assert sr.backfill(args, poster) == 0
    poster.assert_called_once()


def test_concurrent_backfill_posts_once(backfill_case):
    args, _ = backfill_case
    poster = Mock(return_value=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: sr.backfill(args, poster), range(2), timeout=10)) == [0, 0]
    poster.assert_called_once()


def test_backfill_outbox_prevents_forward_replay(backfill_case, monkeypatch):
    from plangraph.spawner.notify import deliver_verdict
    args, run = backfill_case
    poster = Mock(return_value=0)
    assert sr.backfill(args, poster) == 0
    monkeypatch.setattr(sv, "evidence_from_record", lambda *a: pytest.fail("forward replay"))
    deliver_verdict(run, json.loads((run / "record.json").read_text()), None)
    poster.assert_called_once()


@pytest.mark.parametrize("text", ["VERDICT: CHANGES", "VERDICT CHANGES",
                                  "VERDICT: REQUEST_CHANGES", "VERDICT REQUEST_CHANGES"])
def test_changes_does_not_bypass_pass_safety(backfill_case, monkeypatch, text):
    args, run = backfill_case
    data = json.loads((run / "record.json").read_text())
    data.pop("finish_reason")
    (run / "record.json").write_text(json.dumps(data))
    (run / "live.log").write_text(text + "\nTEST_GAMING: yes\n")
    sender = Mock(return_value=(200, "ok"))
    monkeypatch.setattr(sv, "send", sender)
    assert sr.backfill(args, sv.main) == 0
    sender.assert_called_once()
    assert sender.call_args.args[2]["ok"] is False


@pytest.mark.parametrize("text", ["VERDICT: ACCEPT", "VERDICT ACCEPT"])
def test_accept_still_requires_pass_safety(backfill_case, monkeypatch, text):
    args, run = backfill_case
    (run / "live.log").write_text(text + "\nTEST_GAMING: yes\n")
    sender = Mock()
    monkeypatch.setattr(sv, "send", sender)
    assert sr.backfill(args, sv.main) == 0
    sender.assert_not_called()
    assert not (run / "verdict.json").exists()


@pytest.mark.parametrize("changes_first", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_changes_dominates_all_seat_orders_and_replay(backfill_case, monkeypatch,
                                                      changes_first, blocked):
    args, first = backfill_case
    host = args.records_dir / "zz-last"
    host.mkdir()
    second = record(host)
    changes, accept = (first, second) if changes_first else (second, first)
    (changes / "live.log").write_text("VERDICT: CHANGES\nTEST_GAMING: no\n")
    (accept / "live.log").write_text("VERDICT: ACCEPT\nTEST_GAMING: no\n")
    if blocked:
        (changes / "verdict.json").write_text('{"status": "sending"}')
    sender = Mock(return_value=(200, "ok"))
    monkeypatch.setattr(sv, "send", sender)
    for _ in range(2):
        assert sr.backfill(args, sv.main) == 0
    if blocked:
        sender.assert_not_called()
    else:
        sender.assert_called_once()
        assert sender.call_args.args[2]["ok"] is False
        assert (changes / "verdict.delivered").exists()
    assert not (accept / "verdict.json").exists()


@pytest.mark.parametrize("changes,reason", [
    ({"pin": "deepseek/deepseek-v3"}, "pinned seat"),
    ({"spawn_pin": True}, "pinned seat"),
    ({"plan": {"pinned": True}}, "pinned seat"),
    ({"plan": {"p_source": "override"}}, "pinned seat"),
    ({"override_reason": "explicit review override"}, "pinned seat"),
    ({"chosen_leg": {"model": "deepseek/deepseek-v3", "pin": True}}, "pinned seat"),
    ({"served_model": "gpt-5"}, "family same as writer"),
    ({"status": "failed"}, "record status 'failed' is not 'completed'"),
    ({"status": "interrupted"}, "record status 'interrupted' is not 'completed'"),
    ({"status": "running"}, "record status 'running' is not 'completed'"),
    ({"exit_code": 1}, "record exit_code 1 is not 0"),
    ({"exit_code": False}, "record exit_code False is not 0"),
])
def test_unsafe_records_have_reasons_and_never_post(backfill_case, capsys, changes, reason):
    args, run = backfill_case
    path = run / "record.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), **changes)))
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "MISSING"
    assert reason in " ".join(row["reasons"])
    poster.assert_not_called()
    assert not (run / "verdict.json").exists()


@pytest.mark.parametrize("text", ["ACCEPT", "VERDICT: maybe", "VERDICT: pass | fix",
                                   "> VERDICT: ACCEPT", "```\nVERDICT: ACCEPT\n```"])
def test_no_strict_verdict_never_posts(backfill_case, capsys, text):
    args, run = backfill_case
    (run / "live.log").write_text(text + "\nTEST_GAMING: no\n")
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "MISSING"
    assert "no VERDICT" in " ".join(row["reasons"])
    poster.assert_not_called()


@pytest.mark.parametrize("contents", ["{", "null", "[]", "{}", '{"status": "unknown"}',
                                       '{"status": "retry", "attempts": "bad"}'])
def test_ambiguous_journal_skips_only_its_item(backfill_case, capsys, contents):
    args, run = backfill_case
    journal = run / "verdict.json"
    journal.write_text(contents)
    host = args.records_dir / "other"
    host.mkdir()
    healthy = record(host, item_key="demo:B")
    writer = args.records_dir / "writer-b"
    writer.mkdir()
    record(writer, role="implement", item_key="demo:B", served_model="gpt-5")
    snapshot = json.loads(args.snapshot.read_text())
    snapshot["items"].append(dict(snapshot["items"][0], key="demo:B"))
    args.snapshot.write_text(json.dumps(snapshot))
    poster = Mock(return_value=0)
    for _ in range(2):
        assert sr.backfill(args, poster) == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["delivery"] == "skipped"
        assert "ambiguous verdict journal" in " ".join(rows[0]["reasons"])
    poster.assert_called_once()
    assert poster.call_args.args[0][1] == "demo:B"
    assert journal.read_text() == contents
    assert (healthy / "verdict.delivered").exists()


@pytest.mark.parametrize("rc,delivery", [(0, "delivered"), (1, "failed"), (2, "failed")])
def test_json_reports_delivery_and_replay(backfill_case, capsys, rc, delivery):
    args, _ = backfill_case
    poster = Mock(return_value=rc)
    assert sr.backfill(args, poster) == int(bool(rc))
    row = json.loads(capsys.readouterr().out)[0]
    assert row["delivery"] == delivery
    assert row["verifier_exit"] == rc
    assert sr.backfill(args, poster) == 0
    assert json.loads(capsys.readouterr().out)[0]["delivery"] == "skipped"
    poster.assert_called_once()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock contention")
def test_busy_forward_lock_is_bounded_and_never_posts(backfill_case):
    import fcntl
    args, run = backfill_case
    with (run / "verdict.json.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, str(Path(sv.__file__).resolve()),
            "--backfill-apply", "--snapshot", str(args.snapshot), "--records-dir",
            str(args.records_dir), "--url", args.url, "--token-dir", str(args.token_dir),
            "--format", "json"], env=dict(os.environ, HOME=str(args.records_dir)),
            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)[0]
    assert row["delivery"] == "skipped"
    assert "verdict lock busy" in row["reasons"]
    assert not (run / "verdict.json").exists()


def test_ambiguous_journal_is_reported_in_tsv(backfill_case, capsys):
    args, run = backfill_case
    args.format = "tsv"
    (run / "verdict.json").write_text("{")
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    assert "ambiguous verdict journal" in capsys.readouterr().out
    poster.assert_not_called()


@pytest.mark.parametrize("kind", ["journal", "lock"])
def test_symlink_outbox_is_not_followed(backfill_case, capsys, kind):
    args, run = backfill_case
    target = args.records_dir / "outside"
    target.write_text('{"status": "retry", "attempts": 0}')
    name = "verdict.json" if kind == "journal" else "verdict.json.lock"
    (run / name).symlink_to(target)
    poster = Mock()
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["delivery"] == "skipped"
    assert "ambiguous verdict journal" in row["reasons"]
    poster.assert_not_called()
    assert target.read_text() == '{"status": "retry", "attempts": 0}'


def test_apply_gh_lookup_timeout_is_bounded(backfill_case, monkeypatch, capsys):
    args, _ = backfill_case
    args.pr_lookup_gh = True
    calls = []
    def timeout(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["gh", "pr", "list"]
        assert 0 < kwargs["timeout"] <= 30
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timeout)
    poster = Mock(return_value=0)
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["pr_number"] is None
    assert row["delivery"] == "delivered"
    assert len(calls) == 1
    poster.assert_called_once()


def test_keeper_send_wait_is_bounded(monkeypatch):
    calls = []
    def timeout(request, **kwargs):
        calls.append(request)
        assert 0 < kwargs["timeout"] <= 30
        raise TimeoutError("fixture keeper timeout")
    monkeypatch.setattr("urllib.request.urlopen", timeout)
    with pytest.raises(TimeoutError, match="fixture keeper timeout"):
        sv.send("http://fake.invalid", "dummy", {"key": "demo:A", "ok": False})
    assert len(calls) == 1
