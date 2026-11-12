"""Offline decision contract: no subprocess, network, keeper or merge effects."""
import copy
import json
from pathlib import Path

import pytest

SHA = "a" * 40


def test_go(facts, core):
    before = copy.deepcopy(facts)
    result = core.decide(facts)
    assert (result.outcome, result.sha, result.code) == ("GO", SHA, None)
    assert facts == before


def test_ci_red(facts, refuse):
    facts["ci_checks"][0]["conclusion"] = "failure"
    refuse(facts, "ci_red")


def test_ci_pending(facts, refuse):
    facts["ci_checks"][0]["status"] = "in_progress"
    refuse(facts, "ci_pending")


def test_head_moved(facts, refuse):
    facts["head_sha_now"] = "b" * 40
    refuse(facts, "head_moved")


def test_seat_missing(facts, refuse):
    facts["seats"] = []
    refuse(facts, "seat_missing")


def test_seat_pinned(facts, refuse):
    facts["seats"][0]["pinned"] = True
    refuse(facts, "seat_pinned")


def test_seat_family_is_writer(facts, refuse):
    facts["writer_families"].append("xiaomi")
    refuse(facts, "seat_family_is_writer")


@pytest.mark.parametrize("severity", ["MED", "HIGH", "unknown"])
def test_finding_open(facts, severity, refuse):
    facts["seats"][0]["open_findings"] = [severity]
    refuse(facts, "finding_open")


@pytest.mark.parametrize("path", [".github/config", ".docs-map", ".filesize-allow",
    "tools/new_code_allowlist.txt", "deploy/unit", "tools/check_file_size.py",
    "tools/merge_gate.py", "tools/seat_record.py", "rust/x/src/redact.rs",
    "src/plangraph/spawner/auth.py", "TOOLS/SEAT_VERDICT.PY", "../src/x.rs",
    ".filesize-baseline", ".gitleaks.toml", "tools/ci_select.py",
    "tools/preflight.py", "tools/red_proof.py", "src/auth/session.rs",
    "rust/x/redact/pattern.rs", "configuration/allowlists/paths.json",
    "tools/public_scrub.py"])
def test_gate_path(facts, path, refuse):
    facts["changed_paths"] = [path]
    refuse(facts, "gate_path")


def test_tier_high(facts, refuse):
    facts["tier"] = "high"
    refuse(facts, "tier_high")


def test_plan_key_mismatch(facts, refuse):
    facts["plan_key"] = "demo:OTHER"
    refuse(facts, "plan_key_mismatch")


def test_preflight_missing(facts, refuse):
    facts["preflight_present"] = False
    refuse(facts, "preflight_missing")


def test_stale_green(facts, refuse):
    facts["main_changed_production_paths"] = ["src/example.rs"]
    refuse(facts, "stale_green")


@pytest.mark.parametrize("verdict", ["", "VERDICT: acceptable", "> VERDICT: ACCEPT",
    "```\nVERDICT: ACCEPT\n```", "VERDICT: ACCEPT | FIX", "VERDICT: REJECT"])
def test_strict_verdict_reused(facts, verdict, refuse):
    facts["seats"][0]["verdict"] = verdict
    refuse(facts, "seat_missing")


@pytest.mark.parametrize("field,value,code", [
    ("ci_checks", [], "ci_pending"), ("required_checks", [], "ci_pending"),
    ("required_checks", ["ci-gate", "ci-gate"], "ci_pending"),
    ("writer_families", [], "seat_family_is_writer"),
    ("plan_key", "", "plan_key_mismatch"), ("tier", "unknown", "tier_high"),
    ("preflight_present", "true", "preflight_missing"),
    ("changed_paths", None, "gate_path"),
    ("main_changed_production_paths", None, "stale_green")])
def test_fail_closed(facts, field, value, code, refuse):
    facts[field] = value
    refuse(facts, code)


def test_wrong_check_sha_and_red_precedence(facts, refuse):
    facts["ci_checks"][0]["head_sha"] = "b" * 40
    refuse(facts, "ci_pending")
    facts["ci_checks"].append(dict(name="other", head_sha=SHA,
                                    status="completed", conclusion="failure"))
    refuse(facts, "ci_red")


def test_disjoint_main_and_low_findings_go(facts, core):
    facts["main_changed_production_paths"] = ["src/other.rs"]
    facts["seats"][0]["open_findings"] = ["LOW"]
    assert core.decide(facts).outcome == "GO"


def test_shared_policy_is_not_copied(facts, monkeypatch, core, refuse):
    monkeypatch.setattr(core.ci_commit_gate, "GATE_PATHS", ("src/example.rs",))
    refuse(facts, "gate_path")


@pytest.mark.parametrize("red", [False, True])
def test_cli_read_only(facts, tmp_path, capsys, monkeypatch, red, merge_gate):
    facts["preflight_present"] = not red
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(facts))
    before = file.read_bytes()
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("write"))
    assert merge_gate.main([str(file)]) == int(red)
    expected = "REFUSE preflight_missing preflight record absent" if red else f"GO {SHA}"
    assert capsys.readouterr().out == expected + "\n"
    assert file.read_bytes() == before and list(tmp_path.iterdir()) == [file]


def test_cli_bad_json_is_one_safe_refusal(tmp_path, capsys, merge_gate):
    file = tmp_path / "facts.json"
    file.write_text("not json")
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out == "REFUSE ci_pending facts unavailable or invalid\n"


def test_required_check_cannot_be_skipped(facts, refuse):
    facts["ci_checks"][0]["conclusion"] = "skipped"
    refuse(facts, "ci_pending")


def test_unselected_suite_can_be_skipped(facts, core):
    facts["ci_checks"].append(dict(name="unselected", head_sha=SHA,
                                    status="completed", conclusion="skipped"))
    assert core.decide(facts).outcome == "GO"


@pytest.mark.parametrize("field,value,code", [
    ("status", "queued", "ci_pending"), ("conclusion", None, "ci_pending"),
    ("conclusion", "cancelled", "ci_red"), ("conclusion", "neutral", "ci_red"),
    ("conclusion", "timed_out", "ci_red"), ("conclusion", "unknown", "ci_red")])
def test_check_states_fail_closed(facts, field, value, code, refuse):
    facts["ci_checks"][0][field] = value
    refuse(facts, code)


def test_seat_coverage_is_reused(facts, monkeypatch, core, refuse):
    monkeypatch.setattr(core.seat_verdict, "coverage", lambda *a, **kw: ["openai"])
    refuse(facts, "seat_missing")


def test_cli_duplicate_facts_refused(tmp_path, capsys, merge_gate):
    file = tmp_path / "facts.json"
    file.write_text('{"preflight_present":false,"preflight_present":true}')
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out == "REFUSE ci_pending facts unavailable or invalid\n"
