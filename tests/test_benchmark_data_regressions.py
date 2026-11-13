"""Hermetic regressions for the benchmark contract's review findings."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

import test_benchmark_data_contract as contract
from test_benchmark_data_contract import offline_contract, valid_contract  # noqa: F401


def test_pending_file_matches_frozen_ceiling():
    contract._pending_keys(json.loads(contract.PENDING_PATH.read_text(encoding="utf-8")))


@pytest.mark.parametrize("legacy", ["_LEGACY_MISSING", "_LEGACY_UNSOURCED"])
def test_pending_and_legacy_tables_cannot_grow_together(monkeypatch, tmp_path, legacy):
    pending = json.loads(contract.PENDING_PATH.read_text(encoding="utf-8"))
    subject = "leg:example-new@example/new" if legacy == "_LEGACY_MISSING" else "example/new"
    problems = ["missing_row"] if legacy == "_LEGACY_MISSING" else [
        "source_url", "source_date", "source_identity"]
    pending.append({"subject": subject, "effort": None, "problems": problems,
                    "reason": "Synthetic attempt to expand the frozen debt."})
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(pending), encoding="utf-8")
    monkeypatch.setattr(contract, "PENDING_PATH", path)
    monkeypatch.setattr(contract, legacy, getattr(contract, legacy) | {(subject, None)})
    expanded = frozenset(
        {("missing_row", subject, effort) for subject, effort in contract._LEGACY_MISSING}
        | {(problem, subject, effort) for subject, effort in contract._LEGACY_UNSOURCED
           for problem in ("source_url", "source_date", "source_identity")})
    monkeypatch.setattr(contract, "FROZEN_PENDING", expanded)
    with pytest.raises(AssertionError, match="frozen pending ceiling changed"):
        contract.test_pending_list_cannot_grow()


@pytest.mark.parametrize("replace_entry", [False, True])
def test_frozen_ceiling_cannot_shrink_or_substitute(monkeypatch, replace_entry):
    changed = set(contract.FROZEN_PENDING)
    changed.remove(next(iter(changed)))
    if replace_entry:
        changed.add(("missing_row", "leg:example-new@example/new", None))
    monkeypatch.setattr(contract, "FROZEN_PENDING", frozenset(changed))
    with pytest.raises(AssertionError, match="frozen pending ceiling changed"):
        contract.test_pending_list_cannot_grow()


@pytest.mark.parametrize("mutation", ["remove_entry", "remove_problem", "add_entry",
                                    "add_problem", "substitute_entry"])
def test_pending_file_matches_frozen_literal_except_removals(monkeypatch, tmp_path, mutation):
    pending = json.loads(contract.PENDING_PATH.read_text(encoding="utf-8"))
    if mutation == "remove_entry":
        pending.pop()
    elif mutation == "remove_problem":
        next(entry for entry in pending if len(entry["problems"]) > 1)["problems"].pop()
    elif mutation == "add_problem":
        pending[0]["problems"].append("source_url")
    else:
        if mutation == "substitute_entry":
            pending.pop()
        pending.append({"subject": "leg:example-new@example/new", "effort": None,
                        "problems": ["missing_row"], "reason": "Not frozen debt."})
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(pending), encoding="utf-8")
    monkeypatch.setattr(contract, "PENDING_PATH", path)
    if mutation.startswith("remove_"):
        test_pending_file_matches_frozen_ceiling()
    else:
        with pytest.raises(AssertionError, match="pending list grew"):
            test_pending_file_matches_frozen_ceiling()


@pytest.mark.parametrize("name", ["example/model-high", "Example Model (high)"])
@pytest.mark.parametrize("ending", [".", ". Next sentence.", '."', ".)"])
def test_sentence_final_period_accepts_model_id_or_alias(valid_contract, name, ending):
    rows, models, legs = valid_contract
    rows[0]["source_record"] = {"excerpt": "Results for " + name + ending}
    contract._assert_contract(rows, models, legs)


@pytest.mark.parametrize("suffix", ["-next", ".next", ".1", "/next", "_next", "word"])
def test_identifier_continuations_are_not_sentence_punctuation(valid_contract, suffix):
    rows, models, legs = valid_contract
    rows[0]["source_record"] = {"excerpt": "example/model-high" + suffix}
    rows[0]["aliases"] = []
    with pytest.raises(AssertionError, match="source_identity"):
        contract._assert_contract(rows, models, legs)


@pytest.mark.parametrize("mutation, message", [
    ("missing_row", "missing_row"), ("wrong_effort", "missing_row"),
    ("source_url", "source_url"), ("source_date", "source_date"),
    ("compact_date", "source_date"), ("source_identity", "source_identity"),
    ("duplicate_row", "duplicate benchmark"), ("pending_growth", "pending list grew"),
    ("pending_reason", "reason required"), ("pending_duplicate", "duplicate pending"),
    ("resolved_pending", "remove resolved pending"),
])
def test_contract_checks_survive_python_optimization(valid_contract, mutation, message):
    # Compile exactly as python -O would, without subprocess or network access.
    path = Path(contract.__file__)
    namespace = {"__file__": str(path), "__name__": "optimized_benchmark_contract"}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec", optimize=1), namespace)
    rows, models, legs = valid_contract
    pending = []
    if mutation == "missing_row":
        rows = []
    elif mutation == "wrong_effort":
        legs = [replace(legs[0], effort="low")]
    elif mutation in {"source_url", "source_date"}:
        rows[0][mutation] = None
    elif mutation == "compact_date":
        rows[0]["source_date"] = "20261113"
    elif mutation == "source_identity":
        rows[0]["source_record"] = {"title": "Another model"}
    elif mutation == "duplicate_row":
        rows.append(dict(rows[0]))
    else:
        entry = json.loads(contract.PENDING_PATH.read_text(encoding="utf-8"))[0]
        if mutation == "pending_growth":
            entry["subject"] = "leg:example-new@example/new"
        elif mutation == "pending_reason":
            entry["reason"] = " "
        pending = [entry, entry] if mutation == "pending_duplicate" else [entry]
    with pytest.raises(AssertionError, match=message):
        namespace["_assert_contract"](rows, models, legs, pending)
