from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RISKS = ("low", "normal", "high", "critical")
ROLES = ("implement", "driver", "review")
PERTURBATIONS = ("intel_pm1", "cost_pm10pct", "speed_pm10pct")


@contextlib.contextmanager
def isolated_import_state():
    saved_path = list(sys.path)
    module_name = "spawner_sensitivity_under_test"
    had_module = module_name in sys.modules
    try:
        yield
    finally:
        sys.path[:] = saved_path
        if not had_module:
            sys.modules.pop(module_name, None)


@pytest.fixture
def restore_import_state():
    with isolated_import_state():
        yield


def test_docs_contract_terms() -> None:
    text = (ROOT / "docs" / "SPAWNER.md").read_text(encoding="utf-8")
    assert "### Input Contract, Fail-Closed Rules and Sensitivity" in text
    for term in (
        "relax_floor",
        "relax_reason",
        "envelope_intelligence",
        '"drive"',
        "spawner-sensitivity.json",
        "stability_seeds",
        "risk-monotone envelope",
        "0.20",
        "pool_burn_weight",
        "128000",
    ):
        assert term in text

def test_sensitivity_json_shape_and_threshold() -> None:
    data = json.loads(
        (ROOT / "docs" / "spawner-sensitivity.json").read_text(encoding="utf-8")
    )
    assert data["seed"] == 20261022
    assert "catalogs" in data
    assert set(data["perturbations"]) == set(PERTURBATIONS)

    for kind in PERTURBATIONS:
        entries = data["perturbations"][kind]
        for risk in RISKS:
            for role in ROLES:
                key = f"{risk}/{role}"
                assert key in entries
                assert "flip_rate" in entries[key]
                assert "unresolved" in entries[key]

    assert data["perturbations"]["intel_pm1"]["normal/implement"]["flip_rate"] <= 0.10
    assert data["perturbations"]["intel_pm1"]["high/implement"]["flip_rate"] <= 0.10
    assert data["perturbations"]["intel_pm1"]["critical/implement"]["flip_rate"] <= 0.10
    max_by_kind = data["summary"]["max_flip_rate_by_perturbation"]
    assert set(max_by_kind) == set(PERTURBATIONS)

def _load_sweep_module():
    path = ROOT / "tools" / "spawner_sensitivity.py"
    spec = importlib.util.spec_from_file_location("spawner_sensitivity_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

@pytest.mark.usefixtures("restore_import_state")
def test_sensitivity_json_reproducible(tmp_path: Path) -> None:
    committed = (ROOT / "docs" / "spawner-sensitivity.json").read_bytes()
    out = tmp_path / "spawner-sensitivity.json"
    module = _load_sweep_module()
    rc = module.main(["--out", str(out), "--catalogs", "200"])
    assert rc == 0
    assert out.read_bytes() == committed


def test_sweep_loader_does_not_leak_state() -> None:
    saved_path = list(sys.path)
    with isolated_import_state():
        _load_sweep_module()
    assert sys.path == saved_path
    assert "spawner_sensitivity_under_test" not in sys.modules


def test_sweep_is_not_vacuous():
    import json
    from pathlib import Path

    docs_path = Path(__file__).resolve().parents[1] / "docs" / "spawner-sensitivity.json"
    data = json.loads(docs_path.read_text())
    perturbations = data["perturbations"]

    saw_flip = False
    for pert_name, entries in perturbations.items():
        for entry_name, entry in entries.items():
            assert entry["unresolved"] <= 20, (
                f"{pert_name}/{entry_name} unresolved={entry['unresolved']} exceeds 20"
            )
            if entry.get("flip_rate", 0.0) > 0.0:
                saw_flip = True

    assert saw_flip, "sensitivity sweep never flipped; vacuous sweep"


def test_fail_closed_sweep_exercised() -> None:
    data = json.loads(
        (ROOT / "docs" / "spawner-sensitivity.json").read_text(encoding="utf-8")
    )
    fail_closed = data["fail_closed"]
    catalogs = data["catalogs"]
    for risk in RISKS:
        for role in ROLES:
            key = f"{risk}/{role}"
            entry = fail_closed[key]
            assert entry["resolved"] + entry["unresolved"] == catalogs
    # The floor binds rarely on the random catalogs (intel 38..60): only critical shows it.
    assert fail_closed["critical/implement"]["unresolved"] > 0
    for risk in ("low", "normal"):
        entry = fail_closed[f"{risk}/implement"]
        assert entry["unresolved"] <= entry["resolved"]
    assert any(
        fail_closed[f"{risk}/{role}"]["resolved"] > 0
        for risk in RISKS
        for role in ROLES
    )


def test_stability_seeds_meet_threshold() -> None:
    data = json.loads(
        (ROOT / "docs" / "spawner-sensitivity.json").read_text(encoding="utf-8")
    )
    stability = data["stability_seeds"]
    seeds = stability["seeds"]
    assert len(seeds) >= 5
    assert 20261022 in seeds
    rates = stability["intel_pm1_max_flip_rate"]
    for seed in seeds:
        entry = rates[str(seed)]
        assert entry["high"] <= 0.10
        assert entry["critical"] <= 0.10
