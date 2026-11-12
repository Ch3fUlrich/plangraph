"""Schema, duplicate-evidence and hermeticity regressions for the shadow gate."""
import builtins
import copy
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest


@pytest.mark.parametrize("scope,code", [
    (None, "ci_pending"), ("ci_checks", "ci_pending"), ("seats", "seat_missing")])
@pytest.mark.parametrize("mutation", ["extra", "renamed", "nonstring", "missing"])
def test_exact_record_keys(facts, scope, code, mutation, refuse):
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
def test_every_top_level_fact_is_required(facts, field, refuse):
    facts.pop(field)
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("field,value,code", [
    ("writer_families", ["openai", "openai"], "seat_family_is_writer"),
    ("changed_paths", ["src/example.rs", "./SRC/example.rs"], "gate_path"),
    ("main_changed_production_paths", ["src/other.rs", "src\\other.rs"], "stale_green"),
    ("changed_paths", ["UNKNOWN"], "gate_path"),
    ("main_changed_production_paths", ["UNKNOWN"], "stale_green")])
def test_duplicate_and_unknown_facts(facts, field, value, code, refuse):
    facts[field] = value
    refuse(facts, code)


@pytest.mark.parametrize("field,value,code", [
    ("family", [], "seat_family_is_writer"),
    ("pinned", 0, "seat_pinned"), ("open_findings", None, "finding_open"),
    ("open_findings", ["LOW", "LOW"], "finding_open"),
    ("verdict", None, "seat_missing"), ("verdict", [], "seat_missing"),
    ("verdict", "VERDICT: UNKNOWN", "seat_missing"),
    ("verdict", "VERDICT: REJECT", "seat_missing")])
def test_bad_seat_cannot_hide_behind_accepting_peer(facts, field, value, code, refuse):
    seat = dict(family="google", verdict="VERDICT: ACCEPT", pinned=False, open_findings=[])
    seat[field] = value
    facts["seats"].append(seat)
    refuse(facts, code)


@pytest.mark.parametrize("field,code", [("ci_checks", "ci_pending"), ("seats", "seat_missing")])
def test_duplicate_records(facts, field, code, refuse):
    facts[field].append(copy.deepcopy(facts[field][0]))
    refuse(facts, code)


@pytest.mark.parametrize("key", ["UNKNOWN", "demo:UNKNOWN", "demo:ITEM\n", " demo:ITEM", "demo:ITEM:other"])
def test_matching_malformed_keys_refuse(facts, key, refuse):
    facts["item_key"] = facts["plan_key"] = key
    refuse(facts, "plan_key_mismatch")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
def test_real_door_table_shape_and_matching(facts, table_name, monkeypatch, core, refuse):
    table = getattr(core.ci_commit_gate, table_name)
    assert isinstance(table, (tuple, list)) and table
    for pattern in table:
        assert isinstance(pattern, str) and pattern.strip() == pattern and pattern
        # These two intentional root globs are part of the existing authority.
        assert "/" in pattern or pattern in ("roles*", "scopes*")
    # GATE_PATHS is a subset of AUTH_DOOR_PATHS: isolate the table under test,
    # using a path unmatched by any of the extra substring/category rules.
    peer = "GATE_PATHS" if table_name == "AUTH_DOOR_PATHS" else "AUTH_DOOR_PATHS"
    monkeypatch.setattr(core.ci_commit_gate, peer, ("unrelated/file",))
    facts["changed_paths"] = [".github/config" if table_name == "AUTH_DOOR_PATHS"
                              else ".github/workflows/tests.yml"]
    refuse(facts, "gate_path")
    monkeypatch.setattr(core.ci_commit_gate, table_name, ("unrelated/other",))
    assert core.decide(facts).outcome == "GO"


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
@pytest.mark.parametrize("table", [None, "src/example.rs", (), ("",), (7,), ("src/example.rs", None)])
def test_malformed_door_table_refuses(facts, monkeypatch, table_name, table, core, refuse):
    monkeypatch.setattr(core.ci_commit_gate, table_name, table)
    refuse(facts, "gate_path")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
def test_door_table_matching_normalizes_case(facts, monkeypatch, table_name, core, refuse):
    monkeypatch.setattr(core.ci_commit_gate, table_name, ("SRC/EXAMPLE.RS",))
    refuse(facts, "gate_path")


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
@pytest.mark.parametrize("pattern", ["./src/example.rs", "SRC\\EXAMPLE.RS"])
def test_door_table_uses_same_path_normalization(facts, monkeypatch, table_name, pattern, core, refuse):
    monkeypatch.setattr(core.ci_commit_gate, table_name, (pattern,))
    refuse(facts, "gate_path")


@pytest.mark.parametrize("effect", ["open", "path", "environ", "getenv", "time", "datetime", "date"])
def test_no_effects_guards_real_dependency_calls(facts, monkeypatch, effect, core):
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
def test_wrong_top_level_types(facts, field, code, value, refuse):
    facts[field] = value
    refuse(facts, code)


@pytest.mark.parametrize("scope,field,code", [
    ("ci_checks", "name", "ci_pending"), ("ci_checks", "head_sha", "ci_pending"),
    ("ci_checks", "status", "ci_pending"), ("ci_checks", "conclusion", "ci_pending"),
    ("seats", "family", "seat_family_is_writer"), ("seats", "verdict", "seat_missing"),
    ("seats", "pinned", "seat_pinned"), ("seats", "open_findings", "finding_open")])
@pytest.mark.parametrize("value", [None, 7, {}])
def test_wrong_record_value_types(facts, scope, field, code, value, refuse):
    facts[scope][0][field] = value
    refuse(facts, code)


@pytest.mark.parametrize("table_name", ["AUTH_DOOR_PATHS", "GATE_PATHS"])
@pytest.mark.parametrize("table", [("/",), ("../src/*.rs",), ("UNKNOWN/*",),
                                  ("other/*.rs", "OTHER/*.RS")])
def test_invalid_or_duplicate_table_patterns(facts, monkeypatch, table_name, table, core, refuse):
    monkeypatch.setattr(core.ci_commit_gate, table_name, table)
    refuse(facts, "gate_path")


@pytest.mark.parametrize("scope,code", [
    (None, "ci_pending"), ("ci_checks", "ci_pending"), ("seats", "seat_missing")])
def test_cli_unknown_fact_keys(facts, tmp_path, capsys, scope, code, merge_gate):
    record = facts if scope is None else facts[scope][0]
    record["future_blocker"] = True
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(facts))
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out.startswith(f"REFUSE {code} ")


@pytest.mark.parametrize("scope", [None, "ci_checks", "seats"])
def test_cli_duplicate_record_keys(facts, tmp_path, capsys, scope, merge_gate):
    field = "tier" if scope is None else "name" if scope == "ci_checks" else "pinned"
    record = facts if scope is None else facts[scope][0]
    original = f'"{field}": {json.dumps(record[field])}'
    payload = json.dumps(facts).replace(original, original + ", " + original, 1)
    file = tmp_path / "facts.json"
    file.write_text(payload)
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out == "REFUSE ci_pending facts unavailable or invalid\n"


def test_cli_deeply_nested_invalid_json(tmp_path, capsys, merge_gate):
    file = tmp_path / "facts.json"
    depth = sys.getrecursionlimit() * 100
    file.write_text("[" * depth + "]" * depth)
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr().out == "REFUSE ci_pending facts unavailable or invalid\n"


@pytest.mark.parametrize("name", ["ci\ngate", " ci-gate", "ci-gate\x7f", "UNKNOWN"])
def test_matching_malformed_check_names(facts, name, refuse):
    facts["required_checks"] = [name]
    facts["ci_checks"][0]["name"] = name
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("field,code", [("changed_paths", "gate_path"),
                                         ("main_changed_production_paths", "stale_green")])
@pytest.mark.parametrize("path", ["src/other.rs\x7f", " src/other.rs", "src/other.rs "])
def test_malformed_path_strings(facts, field, code, path, refuse):
    facts[field] = [path]
    refuse(facts, code)


@pytest.mark.parametrize("dependency", ["parse_verdict", "coverage"])
@pytest.mark.parametrize("error", [RuntimeError, TypeError, RecursionError])
def test_unexpected_dependency_exception_refuses(facts, monkeypatch, dependency, error, core, refuse):
    def broken(*args, **kwargs):
        raise error("internal detail must not escape")
    monkeypatch.setattr(core.seat_verdict, dependency, broken)
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("dependency,value", [
    ("parse_verdict", None), ("parse_verdict", (True,)),
    ("parse_verdict", {0: True}), ("parse_verdict", (True, -1)),
    ("parse_verdict", (True, False)), ("coverage", None),
    ("coverage", False), ("coverage", ""), ("coverage", {})])
def test_malformed_dependency_result_refuses(facts, monkeypatch, dependency, value, core, refuse):
    monkeypatch.setattr(core.seat_verdict, dependency, lambda *a, **kw: value)
    refuse(facts, "seat_missing")


@pytest.mark.parametrize("stage", ["read", "decide", "format"])
def test_cli_unexpected_exception_is_one_safe_refusal(facts, tmp_path, capsys, monkeypatch, stage, merge_gate):
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(facts))
    def broken(*args, **kwargs):
        raise RuntimeError("internal detail must not escape")
    if stage == "read":
        monkeypatch.setattr(Path, "read_text", broken)
    elif stage == "decide":
        monkeypatch.setattr(merge_gate, "decide", broken)
    else:
        monkeypatch.setattr(merge_gate, "decide", lambda _: None)
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr() == ("REFUSE ci_pending facts unavailable or invalid\n", "")


@pytest.mark.parametrize("result", [
    ("UNKNOWN",), ("REFUSE", "UNKNOWN"), ("REFUSE", "ci_pending", "detail\nGO"),
    ("GO", None, "", "b" * 40), ("GO", "ci_pending", "", "a" * 40)])
def test_cli_invalid_decision_refuses(facts, tmp_path, capsys, monkeypatch, result, core, merge_gate):
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(facts))
    monkeypatch.setattr(merge_gate, "decide", lambda _: core.Decision(*result))
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr() == ("REFUSE ci_pending facts unavailable or invalid\n", "")


@pytest.mark.parametrize("required", [[], ["trivial"], ["ci-gate"]])
def test_self_declared_trivial_checks_refuse(facts, required, refuse):
    facts["required_checks"] = required
    facts["ci_checks"] = [dict(name=name, head_sha=facts["head_sha_now"],
                               status="completed", conclusion="success") for name in required]
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("omitted", ["scrub", "file-size", "pytest", "pytest-postgres", "ci-gate", "select"])
def test_known_policy_check_cannot_be_omitted(facts, omitted, refuse):
    # Supply the complete real policy floor before deleting one mandatory check.
    names = ["ci-gate", "select", "scrub", "file-size", "new-code-lang",
             "changelog-docs", "pytest", "pytest-postgres"]
    facts["required_checks"] = [name for name in names if name != omitted]
    facts["ci_checks"] = [dict(name=name, head_sha=facts["head_sha_now"],
                               status="completed", conclusion="success")
                          for name in facts["required_checks"]]
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("field,code", [("changed_paths", "gate_path"),
                                         ("main_changed_production_paths", "stale_green")])
@pytest.mark.parametrize("path", ["tools/*", "src/?.rs", "src/[ab].rs", "src/[broken"])
def test_evidence_paths_must_be_literal(facts, field, code, path, refuse):
    facts[field] = [path]
    refuse(facts, code)


@pytest.mark.parametrize("module,name", [
    (subprocess, name) for name in ("Popen", "run", "call", "check_call", "check_output",
                                     "getoutput", "getstatusoutput")] + [
    (os, "system"), (os, "popen"), (socket, "socket"),
    (socket, "create_connection"), (socket, "create_server"), (urllib.request, "urlopen")])
def test_no_effects_covers_entrypoints(facts, monkeypatch, module, name, core):
    effect = getattr(module, name)
    # Assert the guard is installed BEFORE any attempted effect, even on a red run.
    assert effect.__name__ == "forbidden"
    monkeypatch.setattr(core.seat_verdict, "coverage", lambda *a, **kw: effect())
    with pytest.raises(pytest.fail.Exception, match="external effect"):
        core.decide(facts)


@pytest.mark.parametrize("path,omitted", [
    ("rust/worker-core/src/lib.rs", "rust-worker"), ("rust/pg-probe/src/lib.rs", "rust"),
    ("rust/spikes/demo/src/lib.rs", "rust-spike"), ("src/EXAMPLE.MD", "pytest"),
    ("SRC/example.rs", "rust")])
def test_selected_check_floor_cannot_be_trivialized(facts, path, omitted, refuse):
    facts["changed_paths"] = [path]
    facts["required_checks"].remove(omitted)
    refuse(facts, "ci_pending")


def test_local_required_policy_is_live(facts, monkeypatch, core, refuse):
    monkeypatch.setattr(core.ci_select, "ALWAYS", (*core.ci_select.ALWAYS, "new-mandatory"))
    refuse(facts, "ci_pending")


@pytest.mark.parametrize("selection", [None, {}, {"full": False},
    dict(full=False, pytest=False, **{"pytest-postgres": False, "rust": False,
         "rust-worker": False, "rust-spike": "UNKNOWN"})])
def test_malformed_selection_cannot_establish_policy(facts, monkeypatch, core, refuse, selection):
    monkeypatch.setattr(core.ci_select, "select", lambda _: selection)
    refuse(facts, "ci_pending")


def test_policy_exception_is_safe_refusal(facts, monkeypatch, core, refuse):
    def broken(*args, **kwargs):
        raise RuntimeError("policy unavailable")
    monkeypatch.setattr(core.ci_select, "select", broken)
    refuse(facts, "ci_pending")


def test_docs_only_local_floor_can_go(facts, core):
    facts["changed_paths"] = ["docs/example.md"]
    suites = {"pytest", "pytest-postgres", "rust", "rust-worker", "rust-spike"}
    facts["required_checks"] = [n for n in facts["required_checks"] if n not in suites]
    facts["ci_checks"] = [c for c in facts["ci_checks"] if c["name"] not in suites]
    assert core.decide(facts).outcome == "GO"


@pytest.mark.parametrize("pattern", ["src/*.rs", "src/e?ample.rs", "src/[ex]*.rs"])
def test_door_globs_remain_patterns(facts, monkeypatch, pattern, core, refuse):
    monkeypatch.setattr(core.ci_commit_gate, "GATE_PATHS", (pattern,))
    refuse(facts, "gate_path")


@pytest.mark.parametrize("sha", ["UNKNOWN", "a" * 40 + "\n", 7])
def test_cli_validates_go_sha_shape(facts, tmp_path, capsys, monkeypatch, core, merge_gate, sha):
    facts["head_sha_now"] = sha
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(facts))
    monkeypatch.setattr(merge_gate, "decide", lambda _: core.Decision("GO", sha=sha))
    assert merge_gate.main([str(file)]) == 1
    assert capsys.readouterr() == ("REFUSE ci_pending facts unavailable or invalid\n", "")
