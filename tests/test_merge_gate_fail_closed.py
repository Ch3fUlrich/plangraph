"""Schema, duplicate-evidence and hermeticity regressions for the shadow gate."""
import builtins
import copy
import datetime
import json
import os
from pathlib import Path
import sys
import time

import pytest

from test_merge_gate import core, facts, merge_gate, no_effects, refuse


@pytest.mark.parametrize("scope,code", [
    (None, "ci_pending"), ("ci_checks", "ci_pending"), ("seats", "seat_missing")])
@pytest.mark.parametrize("mutation", ["extra", "renamed", "nonstring", "missing"])
def test_exact_record_keys(facts, scope, code, mutation):
    record = facts if scope is None else facts[scope][0]
    if mutation in ("renamed", "missing"):
        record.pop(next(iter(record)))
    if mutation != "missing":
        record[7 if mutation == "nonstring" else "future_blocker"] = False
    refuse(facts, code)


@pytest.mark.parametrize("field", [
    "head_sha_at_check", "head_sha_now", "required_checks", "ci_checks", "seats",
    "writer_families", "changed_paths", "tier", "plan_key", "item_key",
    "preflight_present", "main_changed_production_paths"])
def test_every_top_level_fact_is_required(facts, field):
    facts.pop(field)
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("field,value,code", [
    ("writer_families", ["openai", "openai"], "seat_family_is_writer"),
    ("changed_paths", ["src/example.rs", "./SRC/example.rs"], "gate_path"),
    ("main_changed_production_paths", ["src/other.rs", "src\\other.rs"], "stale_green"),
    ("changed_paths", ["UNKNOWN"], "gate_path"),
    ("main_changed_production_paths", ["UNKNOWN"], "stale_green")])
def test_duplicate_and_unknown_facts(facts, field, value, code):
    facts[field] = value
    refuse(facts, code)


@pytest.mark.parametrize("field,value,code", [
    ("family", [], "seat_family_is_writer"),
    ("pinned", 0, "seat_pinned"), ("open_findings", None, "finding_open"),
    ("open_findings", ["LOW", "LOW"], "finding_open"),
    ("verdict", None, "seat_missing"), ("verdict", [], "seat_missing"),
    ("verdict", "VERDICT: UNKNOWN", "seat_missing"),
    ("verdict", "VERDICT: REJECT", "seat_missing")])
def test_bad_seat_cannot_hide_behind_accepting_peer(facts, field, value, code):
    seat = dict(family="google", verdict="VERDICT: ACCEPT", pinned=False, open_findings=[])
    seat[field] = value
    facts["seats"].append(seat)
    refuse(facts, code)


@pytest.mark.parametrize("field,code", [("ci_checks", "ci_pending"), ("seats", "seat_missing")])
def test_duplicate_records(facts, field, code):
    facts[field].append(copy.deepcopy(facts[field][0]))
    refuse(facts, code)


@pytest.mark.parametrize("key", ["UNKNOWN", "demo:UNKNOWN", "demo:ITEM\n", " demo:ITEM", "demo:ITEM:other"])
def test_matching_malformed_keys_refuse(facts, key):
    facts["item_key"] = facts["plan_key"] = key
    refuse(facts, "plan_key_mismatch")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
def test_real_door_table_shape_and_matching(facts, table_name):
    table = getattr(core.ci_commit_gate, table_name)
    assert isinstance(table, (tuple, list)) and table
    for pattern in table:
        assert isinstance(pattern, str) and pattern.strip() == pattern and pattern
        # These two intentional root globs are part of the existing authority.
        assert "/" in pattern or pattern in ("roles*", "scopes*")
    facts["changed_paths"] = [".github/config" if table_name == "AUTH_DOOR_PATHS"
                              else "tools/ci_commit_exempt.txt"]
    refuse(facts, "gate_path")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
@pytest.mark.parametrize("table", [None, "src/example.rs", (), ("",), (7,), ("src/example.rs", None)])
def test_malformed_door_table_refuses(facts, monkeypatch, table_name, table):
    monkeypatch.setattr(core.ci_commit_gate, table_name, table)
    refuse(facts, "gate_path")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
def test_door_table_matching_normalizes_case(facts, monkeypatch, table_name):
    monkeypatch.setattr(core.ci_commit_gate, table_name, ("SRC/EXAMPLE.RS",))
    refuse(facts, "gate_path")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
@pytest.mark.parametrize("pattern", ["./src/example.rs", "SRC\\EXAMPLE.RS"])
def test_door_table_uses_same_path_normalization(facts, monkeypatch, table_name, pattern):
    monkeypatch.setattr(core.ci_commit_gate, table_name, (pattern,))
    refuse(facts, "gate_path")


@pytest.mark.parametrize("effect", ["open", "path", "environ", "getenv", "time", "datetime", "date"])
def test_no_effects_guards_real_dependency_calls(facts, monkeypatch, effect):
    def side_effect(*args, **kwargs):
        if effect == "open":
            with builtins.open(__file__, "rb"):
                pass
        elif effect == "path":
            Path(__file__).read_bytes()
        elif effect == "environ":
            os.environ.get("MERGE_GATE_TEST")
        elif effect == "getenv":
            os.getenv("MERGE_GATE_TEST")
        elif effect == "time":
            time.time()
        elif effect == "datetime":
            datetime.datetime.now()
        else:
            datetime.date.today()
        return []

    monkeypatch.setattr(core.seat_verdict, "coverage", side_effect)
    with pytest.raises(pytest.fail.Exception, match="external effect"):
        core.decide(facts)


@pytest.mark.parametrize("field,code", [
    ("head_sha_at_check", "head_moved"), ("head_sha_now", "head_moved"),
    ("required_checks", "ci_pending"), ("ci_checks", "ci_pending"),
    ("seats", "seat_missing"), ("writer_families", "seat_family_is_writer"),
    ("changed_paths", "gate_path"), ("tier", "tier_high"),
    ("plan_key", "plan_key_mismatch"), ("item_key", "plan_key_mismatch"),
    ("preflight_present", "preflight_missing"), ("main_changed_production_paths", "stale_green")])
@pytest.mark.parametrize("value", [None, 7, "UNKNOWN", {}])
def test_wrong_top_level_types(facts, field, code, value):
    facts[field] = value
    refuse(facts, code)


@pytest.mark.parametrize("scope,field,code", [
    ("ci_checks", "name", "ci_pending"), ("ci_checks", "head_sha", "ci_pending"),
    ("ci_checks", "status", "ci_pending"), ("ci_checks", "conclusion", "ci_pending"),
    ("seats", "family", "seat_family_is_writer"), ("seats", "verdict", "seat_missing"),
    ("seats", "pinned", "seat_pinned"), ("seats", "open_findings", "finding_open")])
@pytest.mark.parametrize("value", [None, 7, {}])
def test_wrong_record_value_types(facts, scope, field, code, value):
    facts[scope][0][field] = value
    refuse(facts, code)


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
@pytest.mark.parametrize("table", [("/",), ("../src/*.rs",), ("UNKNOWN/*",),
                                  ("other/*.rs", "OTHER/*.RS")])
def test_invalid_or_duplicate_table_patterns(facts, monkeypatch, table_name, table):
    monkeypatch.setattr(core.ci_commit_gate, table_name, table)
    refuse(facts, "gate_path")


@pytest.mark.parametrize("scope,code", [
    (None, "ci_pending"), ("ci_checks", "ci_pending"), ("seats", "seat_missing")])
def test_cli_unknown_fact_keys(facts, tmp_path, capsys, scope, code):
    record = facts if scope is None else facts[scope][0]
    record["future_blocker"] = True
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(facts))
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out.startswith(f"REFUSE {code} ")


@pytest.mark.parametrize("scope", [None, "ci_checks", "seats"])
def test_cli_duplicate_record_keys(facts, tmp_path, capsys, scope):
    field = "tier" if scope is None else "name" if scope == "ci_checks" else "pinned"
    record = facts if scope is None else facts[scope][0]
    original = f'"{field}": {json.dumps(record[field])}'
    payload = json.dumps(facts).replace(original, original + ", " + original, 1)
    file = tmp_path / "facts.json"
    file.write_text(payload)
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out == "REFUSE ci_pending facts unavailable or invalid\n"


def test_cli_deeply_nested_invalid_json(tmp_path, capsys):
    file = tmp_path / "facts.json"
    depth = sys.getrecursionlimit() * 100
    file.write_text("[" * depth + "]" * depth)
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out == "REFUSE ci_pending facts unavailable or invalid\n"


@pytest.mark.parametrize("name", ["ci\ngate", " ci-gate", "ci-gate\x7f", "UNKNOWN"])
def test_matching_malformed_check_names(facts, name):
    facts["required_checks"] = [name]
    facts["ci_checks"][0]["name"] = name
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("field,code", [("changed_paths", "gate_path"),
                                         ("main_changed_production_paths", "stale_green")])
@pytest.mark.parametrize("path", ["src/other.rs\x7f", " src/other.rs", "src/other.rs "])
def test_malformed_path_strings(facts, field, code, path):
    facts[field] = [path]
    refuse(facts, code)
