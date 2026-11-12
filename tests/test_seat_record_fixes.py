"""F1-F9 fix round (D-3564): seat_record evidence from a spawner record.json.
Fixtures in tests/fixtures/seat_records are redacted copies of real spawner
artifacts (host paths/pids/stderr stripped, VERDICT/TEST_GAMING layout kept)."""
import json
import shutil
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_record  # noqa: E402
import seat_verdict as sv  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert Path.home() == tmp_path


@pytest.mark.parametrize("value", ["invalid", {}, [], True, -1, 0, 1.5])
def test_invalid_cached_pr_is_unresolved(value):
    assert seat_record.merged_pr({"head_sha": "a" * 40}, None, lambda _: value) is None


@pytest.mark.parametrize("text,count", [
    ("09:30 run:1 sha:abc", 2), ("model:GPT-4", 2), ("Branch:MAIN Team:AI", 2),
    ("demo:B", 1), ("demo:A demo:B", 1)])
def test_prompt_tokens_do_not_lose_other_seats(tmp_path, text, count):
    from test_seat_backfill import ITEM, record
    good = record(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    bad = record(other)
    (bad / "prompt.txt").write_text("Review item demo:A " + text)
    seats, reasons = seat_record.eligible_records(ITEM, tmp_path, {"demo:A", "demo:B"})
    assert good.name in [s["run_id"] for s in seats]
    assert len(seats) == count, reasons
    if count == 2:
        assert reasons == []


def test_backfill_defects_are_not_missing_seats(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"items": [{"key": "demo:A", "state": "in_review"}]}))
    args = SimpleNamespace(snapshot=snapshot, records_dir=tmp_path, backfill_apply=False)
    with patch.object(seat_record, "snapshot_item", side_effect=TypeError("defect")):
        with pytest.raises(TypeError, match="defect"):
            seat_record.backfill(args, None)


def test_cache_read_once_and_poison_does_not_hide_seats(tmp_path, capsys, monkeypatch):
    from test_seat_backfill import ITEM, SHA, record
    from types import SimpleNamespace
    record(tmp_path)
    writer = tmp_path / "writer"
    writer.mkdir()
    record(writer, role="implement", served_model="gpt-5")
    snapshot = tmp_path / "snapshot.json"
    item = dict(ITEM, state="in_review", evidence=[
        dict(kind="commit", verified=True, epoch=1, ref=SHA)])
    snapshot.write_text(json.dumps({"items": [item, item]}))
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({SHA: "poison"}))
    original, reads = Path.read_text, []
    def read(path, *args, **kwargs):
        if path == cache:
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    args = SimpleNamespace(snapshot=snapshot, records_dir=tmp_path, pr_cache=cache,
                           backfill_apply=False, format="json")
    assert seat_record.backfill(args, None) == 0
    rows = json.loads(capsys.readouterr().out)
    assert all(r["status"] == "ELIGIBLE" and r["pr_number"] is None for r in rows)
    assert reads == [cache]

# real-artifact copies live under fixtures/real/ (excluded from the public tree by
# tools/split/manifest.toml); tests that need them skip when the dir is absent.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real" / "seat_records"
needs_real = pytest.mark.skipif(not FIXTURES.is_dir(), reason="real fixtures excluded from public tree")


def copy_fixture(tmp_path, name):
    """Copy a fixture run dir with the 0600/0700 modes the record binding requires."""
    seat = tmp_path / name
    shutil.copytree(FIXTURES / name, seat)
    seat.chmod(0o700)
    for f in seat.iterdir():
        f.chmod(0o600)
    return seat

OMNI_RUN = "run-17e7efa9a08149788618a3d189d437fa"   # real omniroute leg -> D-2905
FREE_RUN = "run-2b6f4c8e1a9d3f7b5e0c4a8d2f6b1e3c"   # free leg, findings after VERDICT
PASS_RUN = "run-9e1d5c3b7a2f6d4b8c0e2a6f4d8b1c3e"   # free leg, PASS + TEST-GAMING
HEAD40 = "a" * 40
FREE_LEG = {"id": "leg-1", "tier": "subscription", "harness": "opencode",
            "model": "opencode/mimo-v2.6-free", "family": "mimo"}
PASS_LOG = "review ok\nVERDICT: PASS\nTEST_GAMING: no\n"
FIX_LOG = "flaky\nVERDICT: FIX\n"

def rec_dir(tmp_path, name, *, live=None, tail=FIX_LOG, leg=FREE_LEG, **over):
    """Compact synthetic spawner record: run_id == dir name, one attempt."""
    d = tmp_path / name
    d.mkdir()
    record = {"run_id": name, "role": "review", "status": "completed",
              "exit_code": 0, "served_model": "unwitnessed", "served_family": "unwitnessed",
              "writer_family": leg.get("family"), "chosen_leg": leg, "finish_reason": "stop",
              "stdout_tail": tail, "completion": {"event": "DONE", "rc": 0, "head_sha": HEAD40},
              "attempts": [{"leg": leg, "status": "completed", "served_model": "unwitnessed",
                            "served_family": "unwitnessed", "stdout_tail": tail}]}
    record.update(over)
    (d / "record.json").write_text(json.dumps(record))
    (d / "record.json").chmod(0o600)  # umask 002 would make it group-writable
    if live is not None:
        (d / "live.log").write_text(live)
    return d

def argv_for(seat, writers="deepseek", head=HEAD40):
    return ["--key", "plangraph/pr-314", "--seat-dir", str(seat),
            "--writer-family", writers, "--token-dir", str(seat.parent),
            "--dry-run", "--head", head]

# F2 + invariant: a free opencode leg witnesses an unwitnessed served model.
def test_free_leg_unwitnessed_pass_posts(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-11111111111111111111111111111111", live=PASS_LOG)
    assert sv.main(argv_for(seat)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "xiaomi"
    assert "witness=record-chosen-leg" in payload["body"]["reason"]

# F3: no record finish_reason -> evidence omits it -> PASS refused (exit 4).
def test_pass_without_finish_reason_refused(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-22222222222222222222222222222222", live=PASS_LOG,
                   finish_reason=None)
    assert sv.main(argv_for(seat)) == 4
    assert "pass refused" in capsys.readouterr().err

# F3: a FAIL verdict posts without any finish field.
def test_fail_without_finish_reason_posts(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-33333333333333333333333333333333", finish_reason=None)
    assert sv.main(argv_for(seat)) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False

# F2 (build-L1 (1)): a non-free leg never witnesses a family (D-2905); the refusal names D-2905 and the leg id/model.
def test_non_free_leg_unwitnessed_refused(tmp_path, capsys):
    leg = {"id": "cx-gpt-6-luna", "tier": "subscription", "harness": "opencode",
           "model": "omniroute/cx-gpt-6-luna", "family": "gpt"}
    seat = rec_dir(tmp_path, "run-44444444444444444444444444444444", leg=leg,
                   writer_family="gpt")
    assert sv.main(argv_for(seat)) == 2
    err = capsys.readouterr().err
    assert "D-2905" in err and "'cx-gpt-6-luna'" in err and "omniroute/cx-gpt-6-luna" in err

# F2: a witnessed served_model is used as before, any family.
def test_witnessed_served_model_any_family_accepted(tmp_path, capsys):
    leg = {"id": "leg-1", "harness": "opencode", "model": "anthropic/claude-3-opus",
           "family": "anthropic"}
    seat = rec_dir(tmp_path, "run-55555555555555555555555555555555", leg=leg,
                   writer_family="anthropic", served_model="anthropic/claude-3-opus",
                   served_family="anthropic")
    assert sv.main(argv_for(seat)) == 0

# build-L1 (2): reviewed writers come ONLY from the CLI --writer-family list;
# record.writer_family is the seat's own family and never names a writer.
def test_record_writer_family_seat_family_cli_other_writer_accepted(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-66666666666666666666666666666666", live=PASS_LOG,
                   writer_family="mimo")  # == seat family, CLI lists deepseek
    assert sv.main(argv_for(seat)) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True

# build-L1 (2): only when the sole CLI writer IS the seat family, seat_verdict
# refuses ("covers no listed writer").
def test_only_cli_writer_equals_seat_family_refused(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-77777777777777777777777777777777", live=PASS_LOG)
    assert sv.main(argv_for(seat, writers="xiaomi")) == 2
    assert "covers no listed writer" in capsys.readouterr().err

# F6: the record is bound to its run dir.
def test_run_id_mismatch_or_missing_refused(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-88888888888888888888888888888888", run_id="run-other")
    assert sv.main(argv_for(seat)) == 2
    assert "run_id" in capsys.readouterr().err
    seat = rec_dir(tmp_path, "run-99999999999999999999999999999999", run_id=None)
    assert sv.main(argv_for(seat)) == 2
    assert "run_id" in capsys.readouterr().err

# F6: record.json must be uid-owned and not group/world-writable.
def test_record_json_group_writable_refused(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", live=PASS_LOG)
    (seat / "record.json").chmod(0o664)
    assert sv.main(argv_for(seat)) == 2
    assert "writable" in capsys.readouterr().err

# F4: stdout_tail accumulates every leg; >1 attempt needs live.log.
def test_two_attempts_without_live_log_refused(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", tail=PASS_LOG,
                   attempts=[{"leg": FREE_LEG, "stdout_tail": "earlier leg"},
                             {"leg": FREE_LEG, "stdout_tail": PASS_LOG}])
    assert sv.main(argv_for(seat)) == 2
    assert "stdout_tail accumulates every leg" in capsys.readouterr().err

# F4: a single-attempt record may fall back to stdout_tail.
def test_one_attempt_tail_fallback_accepted(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-cccccccccccccccccccccccccccccccc",
                   tail="tail finding\nVERDICT: FIX")
    assert sv.main(argv_for(seat)) == 0
    assert "tail finding" in json.loads(capsys.readouterr().out)["body"]["reason"]

# F4: live.log wins over the tail even with several attempts.
def test_two_attempts_with_live_log_accepted(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-dddddddddddddddddddddddddddddddd", live=FIX_LOG,
                   tail="stale earlier-leg VERDICT: PASS",
                   attempts=[{"leg": FREE_LEG, "stdout_tail": "a"},
                             {"leg": FREE_LEG, "stdout_tail": "b"}])
    assert sv.main(argv_for(seat)) == 0

# F8: both TEST_GAMING and TEST-GAMING spellings count.
def test_hyphen_test_gaming_accepted(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                   live="review ok\nVERDICT: PASS\nTEST-GAMING: no\n")
    assert sv.main(argv_for(seat)) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True

# F8 (build-L1 (3)): "VERDICT must be the last line" stays strict (D-3420) until
# routing rules say otherwise; only TEST[_-]GAMING lines may follow it.
def test_text_after_verdict_stays_strict(tmp_path, capsys):
    seat = rec_dir(tmp_path, "run-ffffffffffffffffffffffffffffffff",
                   live=PASS_LOG + "finding after the verdict\n")
    assert sv.main(argv_for(seat)) == 2
    assert "verdict is not the last line" in capsys.readouterr().err

# F9 (record side): record.json unreadable / invalid JSON / not an object.
def test_record_json_broken_refused(tmp_path):
    with pytest.raises(ValueError, match="unreadable"):
        seat_record.evidence_from_record(tmp_path / "run-1234")
    seat = rec_dir(tmp_path, "run-12341234123412341234123412341234")
    (seat / "record.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        seat_record.evidence_from_record(seat)
    (seat / "record.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="not an object"):
        seat_record.evidence_from_record(seat)

# F9: empty review text and missing chosen_leg.model.
def test_no_review_text_refused(tmp_path):
    seat = rec_dir(tmp_path, "run-56785678567856785678567856785678", live="", tail="")
    with pytest.raises(ValueError, match="no review text"):
        seat_record.evidence_from_record(seat)

def test_chosen_leg_model_missing_refused(tmp_path):
    seat = rec_dir(tmp_path, "run-87658765876587658765876587658765", leg={})
    with pytest.raises(ValueError, match="chosen_leg.model missing"):
        seat_record.evidence_from_record(seat)

# F5: --head binds the completion-time HEAD of the run cwd (documented).
def test_head_help_binds_completion_time_head(capsys):
    with pytest.raises(SystemExit):
        sv.main(["--help"])
    assert "completion" in capsys.readouterr().out

# Fixture: real omniroute leg refused by the D-2905 unwitnessed rule.
@needs_real
def test_fixture_omniroute_leg_refused_by_d2905(tmp_path, capsys):
    assert sv.main(argv_for(copy_fixture(tmp_path, OMNI_RUN))) == 2
    err = capsys.readouterr().err
    assert "D-2905" in err and "omniroute/cx-gpt-6-luna" in err

# Fixture: the real live.log has findings after the VERDICT line -> refused.
@needs_real
def test_fixture_findings_after_verdict_refused(tmp_path, capsys):
    assert sv.main(argv_for(copy_fixture(tmp_path, FREE_RUN))) == 2
    assert "verdict is not the last line" in capsys.readouterr().err

# Fixture: free-leg completed review with finish_reason in the record -> posts.
@needs_real
def test_fixture_free_leg_pass_posts(tmp_path, capsys):
    seat = copy_fixture(tmp_path, PASS_RUN)
    assert sv.main(argv_for(seat)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "openai"
    assert payload["body"]["ok"] is True


@pytest.mark.parametrize("evidence", [None, "bad", {}, [None, "bad", {}]])
def test_malformed_evidence_is_missing_not_a_batch_failure(evidence):
    item = seat_record.snapshot_item({"key": "demo:A", "evidence": evidence})
    assert item["head_sha"] is None
    assert item["missing_reason"] == "no commit evidence"


def test_pr_lookup_without_adapter_is_unresolved():
    assert seat_record.merged_pr({"head_sha": HEAD40}, None) is None


def test_local_pr_lookup_timeout_is_bounded(tmp_path, monkeypatch):
    import subprocess
    from unittest.mock import Mock
    calls = []
    def timeout(*args, **kwargs):
        calls.append(args)
        assert 0 < kwargs["timeout"] <= 30
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timeout)
    git = Mock()
    git.repo_path.return_value = tmp_path
    assert seat_record.merged_pr({"head_sha": HEAD40}, git) is None
    assert len(calls) == 1


@pytest.mark.parametrize("family", ["openai", "deepseek"])
def test_discovery_uses_the_evidence_witness_family(tmp_path, monkeypatch, family):
    from test_seat_backfill import ITEM, record
    run = record(tmp_path)
    evidence, text = seat_record.evidence_from_record(run)
    evidence.update(served_model="opencode/gpt-free" if family == "openai" else
                    "opencode/deepseek-free", witness_kind="record-chosen-leg")
    monkeypatch.setattr(seat_record, "evidence_from_record", lambda *a, **k: (evidence, text))
    seats, reasons = seat_record.eligible_records(ITEM, tmp_path)
    if family == "openai":
        assert seats == []
        assert "family same as writer" in " ".join(reasons)
        assert "family=openai" in " ".join(reasons)
    else:
        assert seats[0]["family"] == family


def test_unwitnessed_free_leg_is_discovered(tmp_path):
    from test_seat_backfill import ITEM, record
    record(tmp_path, served_model="unwitnessed", chosen_leg=FREE_LEG)
    seats, reasons = seat_record.eligible_records(ITEM, tmp_path)
    assert [s["family"] for s in seats] == ["xiaomi"], reasons


@pytest.mark.parametrize("role", ["review", "implement"])
def test_system_symlink_outside_records_root_is_allowed(tmp_path, role):
    from test_seat_backfill import ITEM, record
    target = tmp_path / "target"
    target.mkdir()
    records = target / "records"
    records.mkdir()
    record(records, role=role, served_model="gpt-5" if role == "implement" else
           "deepseek/deepseek-v3")
    alias = tmp_path / "system-mount"
    alias.symlink_to(target, target_is_directory=True)
    root = alias / "records"
    if role == "review":
        seats, reasons = seat_record.eligible_records(ITEM, root)
        assert len(seats) == 1, reasons
    else:
        assert seat_record.writer_families(ITEM, HEAD40, root, None) == ["openai"]


def test_unused_ancestry_helper_is_removed():
    assert not hasattr(sv, "card_head_matches")


@pytest.mark.parametrize("location", ["record", "plan", "chosen_leg"])
def test_single_record_cannot_bypass_pin_gate(tmp_path, capsys, location):
    changes = {"pinned": True} if location == "record" else {
        location: dict(FREE_LEG if location == "chosen_leg" else {}, pinned=True)}
    seat = rec_dir(tmp_path, "run-" + "e" * 32, **changes)
    assert sv.main(argv_for(seat)) == sv.EXIT_REFUSED
    assert "pinned seat" in capsys.readouterr().err
