import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from test_seat_verdict_record import record_dir, HEAD_SHA, sv
import seat_backfill as sb
@pytest.fixture
def run(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "repost", item_key="demo:A", given_writer_families=["deepseek"])
    index = argv.index("--writer-family")
    del argv[index:index + 2]
    argv.remove("--dry-run")
    argv += ["--url", "unused", "--repost-transport"]
    seat, calls = Path(argv[argv.index("--seat-dir") + 1]), []
    (seat / "verdict.json").write_text('{"status":"retry","attempts":1,"reason":"keeper verdict transport not configured"}\n')
    monkeypatch.setattr(sv, "send", lambda *a: (calls.append(a) or (200, "recorded")))
    monkeypatch.setattr(sv.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    return argv, seat, calls

def test_diagnosis_documented_transport_repost_command_posts(run):
    argv, seat, calls = run
    command = (Path(__file__).resolve().parents[1] / "docs/seat-verdict-backfill-diagnosis.md").read_text().split("`python3 tools/seat_verdict.py ")[1].split("`")[0]
    values = dict(K="demo:A", RUN=str(seat), URL="unused", DIR=argv[argv.index("--token-dir") + 1])
    assert sv.main([values.get(a, a) for a in command.removesuffix(" [--head FULLSHA]").split()]) == 0
    assert len(calls) == 1 and json.loads((seat / "verdict.json").read_text())["status"] == "delivered"
    assert (seat / "verdict.delivered").exists() and sv.main(argv) == 1 and len(calls) == 1

def test_transport_repost_preserves_subprocess_import_order(run):
    argv, _, _ = run
    code = """import sys
sys.path.insert(0, sys.argv[1] + '/src')
import plangraph.spawner.notify
sys.path.insert(0, sys.argv[1] + '/tools')
import seat_verdict as sv
sys.path.remove(sys.argv[1] + '/src')
sv.send = lambda *a: (200, 'recorded')
before = sys.path[:]
assert sys.argv[1] + '/src' not in before
assert sv.main(sys.argv[2:]) == 0
assert sys.path == before
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1]), *argv], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

def test_transport_repost_token_read_refusal_preserves_retry(run, capsys):
    argv, seat, calls = run
    record = json.loads((seat / "record.json").read_text())
    assert record["given_writer_families"] == ["deepseek"]
    assert sv.model_family(record["chosen_leg"]["model"]) == "xiaomi"
    before = (seat / "verdict.json").read_bytes()
    (Path(argv[argv.index("--token-dir") + 1]) / "reviewer-xiaomi.token").unlink()
    assert sv.main(argv) == sv.EXIT_IO and calls == []
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()
    assert "seat_verdict: repost refused: poster exit 1" in capsys.readouterr().err

@pytest.mark.parametrize("failure", ["missing-url", "empty-url", "dry-run"])
def test_transport_repost_requires_url_without_dry_run(run, monkeypatch, capsys, failure):
    argv, seat, calls = run
    monkeypatch.delenv("PG_URL", raising=False)
    if failure == "missing-url":
        index = argv.index("--url")
        del argv[index:index + 2]
    elif failure == "empty-url":
        argv[argv.index("--url") + 1] = ""
    else:
        argv.append("--dry-run")
    before = (seat / "verdict.json").read_bytes()
    with pytest.raises(SystemExit) as exc: sv.main(argv)
    assert exc.value.code == 2 and "requires --url" in capsys.readouterr().err
    assert calls == [] and (seat / "verdict.json").read_bytes() == before
    assert not (seat / "verdict.json.lock").exists() and not (seat / "verdict.delivered").exists()

def test_transport_repost_evidence_json_refusal_explains_cause(run, capsys):
    argv, seat, calls = run
    (seat / "evidence.json").write_text("{}")
    before = (seat / "verdict.json").read_bytes()
    assert sv.main(argv) == 1 and calls == []
    error = capsys.readouterr().err
    assert "evidence.json exists" in error and "dry-run" not in error
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()

@pytest.mark.parametrize("failure", ["argv", "exit", "reason"])
def test_transport_repost_invalid_argv_preserves_journal_before_apply(run, failure):
    _, seat, calls = run
    journal, row = seat / "verdict.json", {"reasons": []}
    if failure == "reason": journal.write_text('{"status":"retry","attempts":1,"reason":"other"}')
    before = journal.read_bytes()
    def poster(_):
        sb._SEND_STATE.get()["started"] = False
        calls.append(True)
        raise SystemExit(2)
    args = SimpleNamespace(backfill_apply=True, url="unused", token_dir=seat)
    assert sb.apply_backfill(args, {"key": "demo:A", "writer_families": ["deepseek"]},
        {"seat_dir": seat, "head_sha": "-head" if failure == "argv" else HEAD_SHA}, poster, row,
        retry_reason="keeper verdict transport not configured") == 1
    assert journal.read_bytes() == before
    assert calls == ([True] if failure == "exit" else [])
    if failure == "reason": assert "journal is not a transport-only retry" in row["reasons"]

def test_transport_repost_send_raising_keeps_retry_journal(run, monkeypatch):
    argv, seat, calls = run
    before = (seat / "verdict.json").read_bytes()
    def boom(*a):
        calls.append(a)
        raise OSError("network down")
    monkeypatch.setattr(sv, "send", boom)
    assert sv.main(argv) == 1 and len(calls) == 1
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()
    monkeypatch.setattr(sv, "send", lambda *a: (calls.append(a) or (200, "recorded")))
    assert sv.main(argv) == 0 and len(calls) == 2

@pytest.mark.parametrize("rc", [0, 1])
def test_non_repost_apply_backfill_unchanged(run, rc):
    _, seat, _ = run
    (seat / "verdict.json").unlink()
    row = {"reasons": []}
    args = SimpleNamespace(backfill_apply=True, url="unused", token_dir=seat)
    assert sb.apply_backfill(args, {"key": "demo:A", "writer_families": ["deepseek"]},
        {"seat_dir": seat, "head_sha": HEAD_SHA}, lambda _: rc, row) == rc
    assert row["reasons"] == [] and row["delivery"] == ("failed" if rc else "delivered")
    assert json.loads((seat / "verdict.json").read_text())["status"] == ("skipped" if rc else "delivered")

def test_non_repost_poster_systemexit_propagates(run):
    _, seat, _ = run
    (seat / "verdict.json").unlink()
    def poster(_): raise SystemExit(2)
    args = SimpleNamespace(backfill_apply=True, url="unused", token_dir=seat)
    with pytest.raises(SystemExit):
        sb.apply_backfill(args, {"key": "demo:A", "writer_families": ["deepseek"]},
            {"seat_dir": seat, "head_sha": HEAD_SHA}, poster, {"reasons": []})
