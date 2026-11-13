"""Offline benchmark admission contract; no scores or publisher evidence authored here."""
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

from plangraph.catalog_data import SEED_MODELS
from plangraph.planner.legs import CX_EFFORTS, Leg, leg_active, spawnable_legs
from plangraph.routing_support.catalog_seed import SEED_BENCHMARKS

PENDING_PATH = Path(__file__).resolve().parents[1] / "configuration/benchmark_data_pending.json"

# Frozen admission ceiling, NOT generated from live rows or the pending file.
# Remove debt from the JSON when resolved; never extend/rebaseline this set.
_LEGACY_MISSING = {
    ("catalog:omniroute/gemini-3.8-flash", None),
    ("catalog:opencode-go/kimi-k3", None),
    ("leg:agy-claude-opus@agy/claude-opus-4-6-thinking", None),
    ("leg:agy-claude-opus-5-5-low@agy/claude-opus-5-5-low", "low"),
    ("leg:agy-claude-opus-5-5-medium@agy/claude-opus-5-5-medium", "medium"),
    ("leg:agy-claude-sonnet-5-5-low@agy/claude-sonnet-5-5-low", "low"),
    ("leg:agy-gemini-3.8-flash@agy/gemini-3.8-flash", None),
    ("leg:agy-gemini-3.8-flash-high@agy/gemini-3.8-flash-high", "high"),
    ("leg:agy-gemini-3.8-flash-low@agy/gemini-3.8-flash-low", "low"),
    ("leg:agy-gemini-3.8-flash-medium@agy/gemini-3.8-flash-medium", "medium"),
    ("leg:agy-gemini-4-argon@agy/gemini-4-argon-high", "high"),
    ("leg:agy-gpt-oss-120b@agy/gpt-oss-120b-medium", "medium"),
    ("leg:agy-opus@agy/claude-opus-5-5-medium", "medium"),
    ("leg:claude-haiku-5.5@claude/claude-haiku-5-5", None),
    ("leg:cx-gpt-6-luna-high@omniroute/cx-gpt-6-luna-high", "high"),
    ("leg:cx-gpt-6-luna-low@omniroute/cx-gpt-6-luna-low", "low"),
    ("leg:cx-gpt-6-luna-max@omniroute/cx-gpt-6-luna-max", "max"),
    ("leg:cx-gpt-6-luna-medium@omniroute/cx-gpt-6-luna-medium", "medium"),
    ("leg:cx-gpt-6-luna-xhigh@omniroute/cx-gpt-6-luna-xhigh", "xhigh"),
    ("leg:cx-gpt-6.1-sol@omniroute/cx-gpt-6.1-sol", None),
    ("leg:cx-gpt-6.1-sol-high@omniroute/cx-gpt-6.1-sol-high", "high"),
    ("leg:cx-gpt-6.1-sol-low@omniroute/cx-gpt-6.1-sol-low", "low"),
    ("leg:cx-gpt-6.1-sol-max@omniroute/cx-gpt-6.1-sol-max", "max"),
    ("leg:cx-gpt-6.1-sol-medium@omniroute/cx-gpt-6.1-sol-medium", "medium"),
    ("leg:cx-gpt-6.1-sol-xhigh@omniroute/cx-gpt-6.1-sol-xhigh", "xhigh"),
    ("leg:deepseek@omniroute/deepseek-direct-flash", None),
    ("leg:omniroute-deepseek-flash@omniroute/deepseek-flash", None),
    ("leg:omniroute-grok-4.7@omniroute/gc/grok-4.7", None),
    ("leg:omniroute-muse-spark@omniroute/muse-spark-1.3-contributor", None),
    ("leg:opencode-big-pickle@opencode/big-pickle", None),
    ("leg:opencode-console-muse-spark@console-meta/muse-spark-1.3-contributor", None),
    ("leg:opencode-gemini-4-argon@opencode/gemini-4-argon", None),
    ("leg:openrouter-free@openrouter/free", None),
}
_LEGACY_UNSOURCED = {
    ("agy/claude-opus-5-5-high", "high"),
    ("agy/claude-sonnet-5-5-high", "high"),
    ("agy/claude-sonnet-5-5-medium", "medium"),
    ("agy/claude-sonnet-5-5-xhigh", "xhigh"),
    ("agy/gemini-3.8-flash-tiered-high", "high"),
    ("claude/claude-haiku-4-5", None),
    ("cxa/gpt-6-astra-max", "max"),
    ("cxa/gpt-6-luna-high", "high"),
    ("cxa/gpt-6-sol-high", "high"),
    ("cxa/gpt-6-sol-low", "low"),
    ("cxa/gpt-6-sol-max", "max"),
    ("cxa/gpt-6-sol-medium", "medium"),
    ("cxa/gpt-6-sol-xhigh", "xhigh"),
    ("deepseek/deepseek-v4-flash-max", None),
    ("gemini/gemini-4-argon-high", "high"),
    ("mimo/mimo-v2.6-flash", None),
    ("mimo/mimo-v2.6-pro", None),
    ("omniroute/cx-gpt-6-luna", None),
    ("opencode-go/deepseek-v4.1-flash", None),
    ("opencode-go/gemini-4-argon", "high"),
    ("opencode-go/glm-5.2", None),
    ("opencode-go/kimi-k3.1-pro", None),
    ("opencode-go/muse-spark-1.3-contributor", "xhigh"),
    ("opencode-go/qwen3.8-max", None),
    ("qoder/qwen3.8-flash-next", None),
}
FROZEN_PENDING = frozenset(
    {("missing_row", subject, effort) for subject, effort in _LEGACY_MISSING}
    | {(problem, subject, effort) for subject, effort in _LEGACY_UNSOURCED
       for problem in ("source_url", "source_date", "source_identity")})
# Independent literal anchor: editing either legacy table must fail separately.
FROZEN_PENDING_COUNT = 108
FROZEN_PENDING_SHA256 = "b2420c166464ab574b732d3f93fb2a589441a6c3bcda83585ff83c0cce238fd3"


def _effort(model_id):
    suffix = model_id.rsplit("-", 1)[-1]
    return suffix if suffix in CX_EFFORTS else None


def _row_key(row, models):
    model_id = row["model_id"]
    declared = next((m["parameter_effort"] for m in models
                     if m["model_id"] == model_id), _effort(model_id))
    return model_id, row.get("effort", declared)


def _names_model(row):
    record = row.get("source_record")
    if not isinstance(record, dict):
        return False
    # Identifiers/URLs in record metadata are NOT publisher text naming a model.
    text = "\n".join(record.get(field, "") for field in ("title", "excerpt")
                     if isinstance(record.get(field), str))
    aliases = row.get("aliases", [])
    if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases):
        return False
    # A terminal period is prose; a period introducing an id suffix is not.
    return any(re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w/-]|\.[\w./-])",
                         text, re.IGNORECASE)
               for name in (row["model_id"], *aliases))


def _violations(rows, models, legs):
    """Issue-specific keys keep a provenance waiver from hiding missing coverage."""
    issues = set()
    keys = {_row_key(row, models) for row in rows}
    if len(keys) != len(rows):
        raise AssertionError("duplicate benchmark model x effort")
    for model in models:
        key = model["model_id"], model["parameter_effort"]
        if model["status"] == "active" and key not in keys:
            issues.add(("missing_row", "catalog:" + key[0], key[1]))
    for leg in legs:
        # Temporary operator pauses must not waive eventual coverage; no clock I/O.
        if not leg_active(leg, now=float("inf")):
            continue
        model_id = leg.model if "/" in leg.model else f"{leg.harness}/{leg.model}"
        _, effort = _row_key({"model_id": model_id}, models)
        effort = leg.effort or effort
        if (model_id, effort) not in keys:
            issues.add(("missing_row", f"leg:{leg.id}@{model_id}", effort))
    for row in rows:
        model_id, effort = _row_key(row, models)
        url = row.get("source_url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) and url.strip() else None
            valid_url = parsed and parsed.scheme in {"http", "https"} and parsed.hostname
        except ValueError:
            valid_url = False
        if not valid_url:
            issues.add(("source_url", model_id, effort))
        source_date = row.get("source_date")
        try:
            if not isinstance(source_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
                raise ValueError("source_date must be YYYY-MM-DD")
            date.fromisoformat(source_date)
        except ValueError:
            issues.add(("source_date", model_id, effort))
        if not _names_model(row):
            issues.add(("source_identity", model_id, effort))
    return issues


@pytest.fixture(autouse=True)
def offline_contract(monkeypatch):
    import os
    import socket
    import subprocess
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("benchmark contract must not use network or subprocesses")

    for module, names in (
        (subprocess, ("Popen", "run", "call", "check_call", "check_output")),
        (os, ("system", "popen")),
        (socket, ("socket", "create_connection", "getaddrinfo")),
        (urllib.request, ("urlopen",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)


def _pending_keys(pending):
    if not isinstance(pending, list):
        raise AssertionError("pending must be one explicit list")
    keys = set()
    for entry in pending:
        if not isinstance(entry, dict) or set(entry) != {"problems", "subject", "effort", "reason"}:
            raise AssertionError("pending entry fields required")
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise AssertionError("pending reason required")
        if not isinstance(entry["problems"], list) or not entry["problems"]:
            raise AssertionError("pending problems required")
        for problem in entry["problems"]:
            key = problem, entry["subject"], entry["effort"]
            if key in keys:
                raise AssertionError("duplicate pending entry")
            keys.add(key)
    if not keys <= FROZEN_PENDING:
        raise AssertionError(f"pending list grew: {keys - FROZEN_PENDING}")
    return keys


def _assert_contract(rows, models, legs, pending=()):
    issues = _violations(rows, models, legs)
    allowed = _pending_keys(list(pending))
    if issues - allowed:
        raise AssertionError(f"unlisted benchmark violations: {issues - allowed}")
    if allowed - issues:
        raise AssertionError(f"remove resolved pending entries: {allowed - issues}")


def test_catalog_benchmark_contract(monkeypatch, tmp_path):
    # No checkout metadata or executable lookup is available to the contract.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    _assert_contract(SEED_BENCHMARKS, SEED_MODELS, spawnable_legs(), pending)


def test_pending_list_cannot_grow():
    canonical = json.dumps(sorted(FROZEN_PENDING), separators=(",", ":")).encode("utf-8")
    if (len(FROZEN_PENDING) != FROZEN_PENDING_COUNT
            or hashlib.sha256(canonical).hexdigest() != FROZEN_PENDING_SHA256):
        raise AssertionError("frozen pending ceiling changed; do not rebaseline")
    _pending_keys(json.loads(PENDING_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def valid_contract():
    # Synthetic identity/citation only; these are not new benchmark measurements.
    model_id = "example/model-high"
    row = {"model_id": model_id, "effort": "high", "aliases": ["Example Model (high)"],
           "source_url": "https://example.invalid/evaluation", "source_date": "2026-11-13",
           "source_record": {"title": "Example Model (high)", "excerpt": "Evaluation results."}}
    model = {"model_id": model_id, "parameter_effort": "high", "status": "active"}
    leg = Leg("example-high", "free", "opencode", model_id, "example", ("128k",),
              100000, False, effort="high")
    return [row], [model], [leg]


def test_model_id_or_listed_alias_is_accepted(valid_contract):
    rows, models, legs = valid_contract
    _assert_contract(rows, models, legs)
    rows[0]["source_record"] = {"excerpt": rows[0]["model_id"]}
    rows[0]["aliases"] = []
    _assert_contract(rows, models, legs)


@pytest.mark.parametrize("catalog", [False, True])
def test_active_leg_without_benchmark_row_fails(valid_contract, catalog):
    _, models, legs = valid_contract
    with pytest.raises(AssertionError, match="missing_row"):
        _assert_contract([], models if catalog else [], [] if catalog else legs)


@pytest.mark.parametrize("effort", CX_EFFORTS)
def test_each_active_leg_effort_requires_its_own_row(valid_contract, effort):
    rows, models, legs = valid_contract
    legs = [replace(legs[0], effort=effort)]
    if effort == "high":
        _assert_contract(rows, models, legs)
    else:
        with pytest.raises(AssertionError, match="missing_row"):
            _assert_contract(rows, models, legs)


@pytest.mark.parametrize("field", ["source_url", "source_date"])
@pytest.mark.parametrize("value", [None, "", " ", "invalid"])
def test_missing_or_invalid_citation_fails(valid_contract, field, value):
    rows, models, legs = valid_contract
    rows[0][field] = value
    with pytest.raises(AssertionError, match=field):
        _assert_contract(rows, models, legs)


@pytest.mark.parametrize("field", ["source_url", "source_date"])
def test_omitted_citation_fails(valid_contract, field):
    rows, models, legs = valid_contract
    del rows[0][field]
    with pytest.raises(AssertionError, match=field):
        _assert_contract(rows, models, legs)


@pytest.mark.parametrize("record", [None, {}, {"title": "Another model"},
    {"model_id": "example/model-high", "source_url": "https://example.invalid/example/model-high"},
    {"excerpt": "example/model-high-next and Example Model (high)-next"}])
def test_source_record_must_name_model_or_alias(valid_contract, record):
    rows, models, legs = valid_contract
    rows[0]["source_record"] = record
    with pytest.raises(AssertionError, match="source_identity"):
        _assert_contract(rows, models, legs)


def test_unlisted_alias_is_not_inferred(valid_contract):
    rows, models, legs = valid_contract
    rows[0]["aliases"] = []
    with pytest.raises(AssertionError, match="source_identity"):
        _assert_contract(rows, models, legs)


def test_disabled_legs_do_not_require_rows_but_expiring_pauses_do(valid_contract):
    _, _, legs = valid_contract
    _assert_contract([], [], [replace(legs[0], disabled="permanent exclusion")])
    with pytest.raises(AssertionError, match="missing_row"):
        _assert_contract([], [], [replace(legs[0], disabled="temporary pause", expires=9999999999)])


def test_pending_rejects_growth_even_after_an_entry_is_removed():
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    pending.pop()
    pending.append({"problems": ["missing_row"], "subject": "leg:new", "effort": "high",
                    "reason": "Not part of the frozen legacy debt."})
    with pytest.raises(AssertionError, match="pending list grew"):
        _pending_keys(pending)


def test_pending_requires_unique_entries_with_reasons():
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError, match="duplicate pending"):
        _pending_keys([pending[0], pending[0]])
    with pytest.raises(AssertionError, match="reason required"):
        _pending_keys([dict(pending[0], reason=" ")])


def test_resolved_pending_entries_must_be_removed():
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError, match="remove resolved pending"):
        _assert_contract([], [], [], [pending[0]])
    _assert_contract([], [], [], [])


def test_provenance_pending_does_not_waive_missing_rows():
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    rows = [r for r in SEED_BENCHMARKS if r["model_id"] != "mimo/mimo-v2.6-pro"]
    with pytest.raises(AssertionError, match="missing_row"):
        _assert_contract(rows, SEED_MODELS, spawnable_legs(), pending)


def test_legacy_missing_row_pending_cannot_hide_retargeted_leg():
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    legs = [replace(leg, model="example/unmeasured") if leg.id == "openrouter-free" else leg
            for leg in spawnable_legs()]
    with pytest.raises(AssertionError, match="missing_row"):
        _assert_contract(SEED_BENCHMARKS, SEED_MODELS, legs, pending)


@pytest.mark.parametrize("effort", [None, "low", "medium", "xhigh", "max"])
def test_unspecified_or_other_effort_row_cannot_cover_high(valid_contract, effort):
    rows, models, legs = valid_contract
    rows[0]["effort"] = effort
    with pytest.raises(AssertionError, match="missing_row"):
        _assert_contract(rows, models, legs)


def test_pending_allows_real_partial_resolution():
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    rows = [dict(row) for row in SEED_BENCHMARKS]
    rows[0]["source_url"] = "https://example.invalid/evaluation"
    entry = next(entry for entry in pending if entry["subject"] == rows[0]["model_id"])
    entry["problems"].remove("source_url")
    _assert_contract(rows, SEED_MODELS, spawnable_legs(), pending)
