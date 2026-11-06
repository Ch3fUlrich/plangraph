"""Frozen material and review ledger contracts."""
import hashlib
import json
from pathlib import Path

import pytest

from tools import plan_loop_ledger as ledger


def reply(line="", verdict="ACCEPT"):
    return f"```plan-review\n{line}\n```\nVERDICT: {verdict}"


@pytest.fixture
def directory(tmp_path):
    (tmp_path / "plan.md").write_text("First plan\n")
    return tmp_path


def test_directory_refuses_checkout():
    with pytest.raises(ValueError):
        ledger.validate_directory(Path(__file__).resolve().parent)


def test_freeze_hash_snapshot_and_drift(directory):
    (directory / "evidence").mkdir()
    (directory / "evidence" / "note").write_bytes(b"proof")
    manifest = ledger.freeze(directory)
    entries = {"plan.md": b"First plan\n", "evidence/note": b"proof"}
    expected = "".join(f"{hashlib.sha256(entries[p]).hexdigest()}  {p}\n" for p in sorted(entries))
    assert manifest["text_sha"] == hashlib.sha256(expected.encode()).hexdigest()
    assert (directory / "frozen" / manifest["text_sha"] / "plan.md").read_bytes() == entries["plan.md"]
    ledger.require_frozen(directory)
    (directory / "plan.md").write_text("changed")
    with pytest.raises(ValueError):
        ledger.require_frozen(directory)


@pytest.mark.parametrize("name", ["data.export.txt", "huge"])
def test_freeze_refuses_unsafe_material(directory, name):
    (directory / "evidence").mkdir()
    (directory / "evidence" / name).write_text("x" * 20)
    with pytest.raises(ValueError):
        ledger.freeze(directory, max_file_chars=15)
    assert not (directory / "manifest.json").exists()


@pytest.mark.parametrize("text,reason", [
    ("VERDICT: ACCEPT", "invalid_block"),
    (reply() + "\n" + reply(), "invalid_block"),
    ("```plan-review\nVERDICT: ACCEPT\n```", "invalid_finding"),
    (reply("bad line"), "invalid_finding"),
    (reply() + "\n[... output truncated ...]", "output_truncated"),
    ("```plan-review\n```", "no_verdict"),
    (reply() + "\nafterword", "no_verdict"),
])
def test_failed_replies(text, reason):
    assert ledger.parse_reply(text)["reason"] == reason


def test_findings_and_last_attempt():
    parsed = ledger.parse_reply(reply("F2-1 | major | state | p:1 | claim | UNVERIFIED | fix | SCOPE+"))
    assert parsed["verdict"] == "REQUEST_CHANGES"
    rows = []
    assert ledger.ingest(rows, parsed, round=2, seat_id="s1", family="alpha", lens="state", sha="a") == ["state"]
    assert rows[0]["id"] == "R2.s1.1"
    assert rows[0]["unverified"] and rows[0]["scope_plus"]
    stripped = ledger.for_seats(rows)[0]
    assert stripped["id"] == "R2.1"
    assert not {"family", "lens", "seat_id"} & stripped.keys()
    assert ledger.parse_record({"stdout_tail": reply(), "attempts": [{"stdout_tail": "noise"}]})["status"] == "failed"


def test_dispositions_and_minor_freeze(directory):
    first = ledger.freeze(directory)
    rows = []
    ledger.ingest(rows, ledger.parse_reply(reply("F1-1 | minor | cost | p | claim | proof | fix")),
                  round=1, seat_id="s", family="alpha", lens="cost", sha=first["text_sha"])
    with pytest.raises(ValueError):
        ledger.dispose(rows, rows[0]["id"], "accepted", first, sha_fixed="unknown")
    (directory / "plan.md").write_text("Small fix\n")
    with pytest.raises(ValueError):
        ledger.freeze(directory, minor=True, fixes=[rows[0]["id"]], rows=rows, minor_max_chars=1)
    second = ledger.freeze(directory, minor=True, fixes=[rows[0]["id"]], rows=rows)
    assert rows[0]["sha_fixed"] == second["text_sha"]
    assert rows[0]["closed_by"] == ""
    ledger.close_accepted(rows, second, [{"status": "completed", "witnessed": True, "sha": second["text_sha"], "run_id": "run"}])
    assert rows[0]["closed_by"] == "run"
    assert second["history"][-1]["material"] is False
    assert json.loads((directory / "ledger.json").read_text())[0]["disposition"] == "accepted"


def test_dispose_guards():
    rows = []
    ledger.ingest(rows, ledger.parse_reply(reply("F1-1 | blocker | state | p | claim | proof | fix")),
                  round=1, seat_id="s", family="alpha", lens="state", sha="a")
    manifest = {"history": [{"sha": "a"}, {"sha": "b"}], "text_sha": "b"}
    for disposition, kwargs in [("rejected", {}), ("deferred", {}), ("accepted", {"sha_fixed": "a"}),
                                ("rejected", {"disposition_evidence": "proof"})]:
        with pytest.raises(ValueError):
            ledger.dispose(rows, rows[0]["id"], disposition, manifest, **kwargs)
    ledger.dispose(rows, rows[0]["id"], "rejected", manifest, disposition_evidence="proof",
                   third_party="r", planner_family="alpha", seats=[{"run_id": "r", "family": "beta", "status": "completed"}])
    assert rows[0]["disposition"] == "rejected"


def test_minor_major_and_discovery_baseline(directory):
    first = ledger.freeze(directory)
    baseline = ledger.start_discovery(directory)["plan_chars_r1"]
    rows = []
    ledger.ingest(rows, ledger.parse_reply(reply("F1-1 | major | state | p | claim | UNVERIFIED | fix")),
                  round=1, seat_id="s", family="alpha", lens="state", sha=first["text_sha"])
    (directory / "plan.md").write_text("Changed plan\n")
    with pytest.raises(ValueError):
        ledger.freeze(directory, minor=True, fixes=[rows[0]["id"]], rows=rows)
    second = ledger.freeze(directory)
    assert ledger.start_discovery(directory)["plan_chars_r1"] == baseline
    with pytest.raises(ValueError):
        ledger.dispose(rows, rows[0]["id"], "accepted", second, sha_fixed=second["text_sha"], tier="high")
    ledger.dispose(rows, rows[0]["id"], "accepted", second, sha_fixed=second["text_sha"], tier="high",
                   fact_check="C1", claims={"C1": {"sha": second["text_sha"], "state": "verified"}})
