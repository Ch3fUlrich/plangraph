from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RISKS = ("low", "normal", "high", "critical")
ROLES = ("implement", "driver", "review")
PERTURBATIONS = ("intel_pm1", "cost_pm10pct", "speed_pm10pct")

def test_docs_contract_terms() -> None:
    text = (ROOT / "docs" / "SPAWNER.md").read_text(encoding="utf-8")
    assert "### Input Contract, Fail-Closed Rules and Sensitivity" in text
    for term in (
        "relax_floor",
        "relax_reason",
        "envelope_intelligence",
        '"drive"',
        "spawner-sensitivity.json",
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

def test_sensitivity_json_reproducible(tmp_path: Path) -> None:
    committed = (ROOT / "docs" / "spawner-sensitivity.json").read_bytes()
    out = tmp_path / "spawner-sensitivity.json"
    module = _load_sweep_module()
    rc = module.main(["--out", str(out), "--catalogs", "200"])
    assert rc == 0
    assert out.read_bytes() == committed


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
