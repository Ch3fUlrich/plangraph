import hashlib
import json

import pytest

from tools import plan_loop_ledger as ledger
from tools import plan_loop_report as report


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize('change', ['tier', 'journal', 'claims', 'extension'])
def test_batch_uses_driver_state(plan, change):
    from tools import plan_loop as cli
    directory, _ = plan
    _, manifest, rows, yields, _ = cli.load(directory)
    if change == 'tier':
        manifest['tier'] = 'high'
        yields[0].update(kind='stacked', lens='scope-cut')
    elif change == 'journal':
        rows.append(dict(severity='blocker', disposition='open'))
    elif change == 'claims':
        manifest['claims']['C1']['state'] = 'open-question'
        manifest['tier'] = 'high'
        (directory / 'r0').mkdir()
        write_json(directory / 'r0/claims.json', {'C1': {'state': 'verified'}})
    else:
        yields[:] = [dict(yields[0], round=i, kind='stacked') for i in range(1, 7)]
        manifest['extra_rounds'] = 2
    cli.save(directory, manifest, rows, yields)
    actual = report._state(directory)[0]
    expected = cli.load(directory)[-1]
    assert actual == expected
    assert actual.state == 'OPEN'


@pytest.fixture
def plan(tmp_path):
    (tmp_path / "plan.md").write_text("A small plan.\n")
    manifest = ledger.freeze(tmp_path)
    sha = manifest["text_sha"]
    manifest.update(slug='sample', tier='standard', tags=[], plan_chars_r1=14, plan_chars=14,
                    report={"sha": sha, "ok": True, "claims": ["C1"]},
                    claims={"C1": {"state": "verified"}})
    write_json(tmp_path / "manifest.json", manifest)
    write_json(tmp_path / "ledger.json", [])
    from pathlib import Path
    policy = Path('tools/plan-loop.default.toml').read_text().replace('accept_families = 4', 'accept_families = 1')
    (tmp_path / "plan-loop.toml").write_text(policy)
    seat = dict(round=1, kind="scope-cut", family="qwen", sha=sha,
                status="completed", witnessed=True, verdict="ACCEPT",
                counts={}, new_classes=[])
    (tmp_path / "yield.jsonl").write_text(json.dumps(seat) + "\n")
    batch = tmp_path / "batch.yaml"
    batch.write_text(f"batch: sample\nprincipal: author\nsource: plan.md\n"
                     f"source_sha256: {sha}\nops:\n"
                     "  - op: create\n    item:\n"
                     "      - {project: demo, id: A1}\n"
                     "      - {project: demo, id: A2}\n")
    return tmp_path, batch


def test_check_records_digest_and_each_key(plan):
    directory, batch = plan
    code, lines = report.check_batch(directory, batch)
    assert code == 0
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()
    assert lines[0] == f"ops_sha={digest}"
    assert "other than" in lines[1] and "D-row" in lines[1]
    records = json.loads((directory / "batches.json").read_text())
    assert records == [{"ops_sha": digest, "op_keys": ["demo:A1", "demo:A2"]}]
    assert report.check_batch(directory, batch)[0] == 0
    assert len(json.loads((directory / "batches.json").read_text())) == 1


@pytest.mark.parametrize("change", ["source", "text", "open", "ops", "duplicate"])
def test_check_refuses_without_recording(plan, change):
    directory, batch = plan
    text = batch.read_text()
    if change == "source":
        text = text.replace(json.loads((directory / "manifest.json").read_text())["text_sha"], "0" * 64)
    elif change == "text":
        (directory / "plan.md").write_text("Edited plan")
    elif change == "open":
        (directory / "yield.jsonl").write_text("")
    elif change == "ops":
        text = text.replace("op: create", "op: unknown")
    else:
        text += "principal: another\n"
    batch.write_text(text)
    assert report.check_batch(directory, batch)[0] == 1
    assert not (directory / "batches.json").exists()


def test_waiver_requires_explicit_permission(plan):
    directory, batch = plan
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["waived"] = "D-1234"
    write_json(path, manifest)
    assert report.check_batch(directory, batch)[0] == 1
    code, lines = report.check_batch(directory, batch, waived_ok=True)
    assert code == 0
    assert any("WAIVED" in line and "--waived-ok" in line for line in lines)


def test_ops_edit_needs_new_digest(plan):
    directory, batch = plan
    assert report.check_batch(directory, batch)[0] == 0
    batch.write_text(batch.read_text().replace("demo, id: A2", "demo, id: A3"))
    assert report.check_batch(directory, batch)[0] == 0
    records = json.loads((directory / "batches.json").read_text())
    assert len({r["ops_sha"] for r in records}) == 2


def test_report_matches_nested_records_and_is_read_only(plan):
    directory, batch = plan
    assert report.check_batch(directory, batch)[0] == 0
    log = directory / "changelog.json"
    write_json(log, {"entries": [
        {"type": "created", "key": "demo:A1", "by": "author", "ts": "today"},
        {"type": "created", "key": "demo:A9", "by": "writer", "ts": "today"},
        {"type": "closed", "key": "demo:A8"},
    ]})
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    code, lines = report.report(directory.parent, log)
    assert code == 0
    assert lines == ["UNMATCHED demo:A9 by=writer ts=today", report.COVERAGE]
    assert before == {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_report_always_prints_coverage(tmp_path):
    log = tmp_path / "log.json"
    write_json(log, {"entries": []})
    assert report.report(tmp_path, log) == (0, [report.COVERAGE])


@pytest.mark.parametrize("bad", ["missing", "json", "shape", "records"])
def test_report_unreadable_input(tmp_path, bad):
    log = tmp_path / "log.json"
    if bad != "missing":
        log.write_text("{" if bad == "json" else '{"entries": []}')
    if bad == "shape":
        write_json(log, {"entries": [{}]})
    if bad == "records":
        write_json(tmp_path / "batches.json", {})
    code, lines = report.report(tmp_path, log)
    assert code == 1
    assert lines[-1] == report.COVERAGE


def test_check_inside_checkout_refused():
    assert report.check_batch(".", "unused.yaml")[0] == 1


def test_check_concurrent_edit_refused(plan, monkeypatch):
    directory, batch = plan
    original = report.apply_batch.load_batch
    def load(path):
        data = original(path)
        path.write_text(path.read_text() + '\n')
        return data
    monkeypatch.setattr(report.apply_batch, 'load_batch', load)
    assert report.check_batch(directory, batch) == (1, ['refused: batch changed during check'])
    assert not (directory / 'batches.json').exists()


@pytest.mark.parametrize('bad', ['missing', 'syntax', 'records'])
def test_check_invalid_input_exit(plan, bad):
    directory, batch = plan
    if bad == 'missing':
        batch.unlink()
    elif bad == 'syntax':
        batch.write_text('[')
    else:
        write_json(directory / 'batches.json', {})
    assert report.check_batch(directory, batch) == (1, ['refused: unreadable or invalid check input'])
