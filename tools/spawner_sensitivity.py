#!/usr/bin/env python3
"""Deterministic, hermetic sensitivity sweep for plangraph.spawner.resolver."""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from plangraph.spawner.resolver import resolve_model_for_task  # noqa: E402

SEED = 20261022
STABILITY_SEEDS = (20261022, 11, 22, 33, 44)
RISKS = ("low", "normal", "high", "critical")
ROLES = ("implement", "driver", "review")
TIERS = ("subscription", "paid", "free", "credit", "trial")
PERTURBATIONS = ("intel_pm1", "cost_pm10pct", "speed_pm10pct")

class FakeDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]

    def list_models(self, active_only: bool = False) -> list[dict[str, Any]]:
        # Every generated row has available=1, so active_only does not matter.
        return [dict(row) for row in self._rows]

def _make_catalog(rng: random.Random, catalog_idx: int) -> list[dict[str, Any]]:
    n_rows = rng.randint(6, 12)
    rows: list[dict[str, Any]] = []
    for row_idx in range(n_rows):
        rows.append(
            {
                "id": f"fam{row_idx % 5}/m{catalog_idx:04d}-{row_idx:02d}",
                "aa_intelligence": rng.uniform(38.0, 60.0),
                "cost_per_task_usd": rng.uniform(0.05, 3.0),
                "pool_burn_weight": rng.uniform(0.5, 1.5),
                "verified_tok_per_s": rng.uniform(1.0, 300.0),
                "tier": rng.choice(TIERS),
                "available": 1,
                "context_window": 1000000,
                "combos": ["l2-worker"],
            }
        )
    return rows

def _resolve_id(role: str, risk: str, rows: list[dict[str, Any]]) -> str | None:
    try:
        picked = resolve_model_for_task(
            role=role,
            risk=risk,
            combo_name="l2-worker",
            db=FakeDB(rows),
            relax_floor=True,
            relax_reason="sweep",
        )
    except ValueError:
        return None
    if isinstance(picked, dict):
        value = picked.get("model")
        if isinstance(value, str):
            return value
    return None

def _resolve_id_fail_closed(
    role: str, risk: str, rows: list[dict[str, Any]]
) -> str | None:
    try:
        picked = resolve_model_for_task(
            role=role,
            risk=risk,
            combo_name="l2-worker",
            db=FakeDB(rows),
            relax_floor=False,
        )
    except ValueError:
        return None
    if isinstance(picked, dict):
        value = picked.get("model")
        if isinstance(value, str):
            return value
    return None

def _resolve_catalog(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str | None]:
    return {
        (risk, role): _resolve_id(role, risk, rows)
        for risk in RISKS
        for role in ROLES
    }

def _perturb_rows(
    rows: list[dict[str, Any]], kind: str, rng: random.Random
) -> list[dict[str, Any]]:
    out = [dict(row) for row in rows]
    if kind == "intel_pm1":
        for row in out:
            row["aa_intelligence"] = row["aa_intelligence"] + rng.uniform(-1.0, 1.0)
    elif kind == "cost_pm10pct":
        for row in out:
            row["cost_per_task_usd"] = row["cost_per_task_usd"] * rng.uniform(0.9, 1.1)
    elif kind == "speed_pm10pct":
        for row in out:
            row["verified_tok_per_s"] = row["verified_tok_per_s"] * rng.uniform(0.9, 1.1)
    else:
        raise ValueError(f"unknown perturbation: {kind}")
    return out

def run(catalogs: int, seed: int = SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    stats: dict[str, dict[str, dict[str, float | int]]] = {
        pert: {
            f"{risk}/{role}": {"flips": 0, "unresolved": 0}
            for risk in RISKS
            for role in ROLES
        }
        for pert in PERTURBATIONS
    }
    fail_closed: dict[str, dict[str, int]] = {
        f"{risk}/{role}": {"resolved": 0, "unresolved": 0}
        for risk in RISKS
        for role in ROLES
    }

    for catalog_idx in range(catalogs):
        rows = _make_catalog(rng, catalog_idx)
        baseline = _resolve_catalog(rows)
        baseline_fail_closed = {
            (risk, role): _resolve_id_fail_closed(role, risk, rows)
            for risk in RISKS
            for role in ROLES
        }
        for risk in RISKS:
            for role in ROLES:
                key = f"{risk}/{role}"
                if baseline_fail_closed[(risk, role)] is None:
                    fail_closed[key]["unresolved"] += 1
                else:
                    fail_closed[key]["resolved"] += 1

        for pert in PERTURBATIONS:
            perturbed_rows = _perturb_rows(rows, pert, rng)
            perturbed = _resolve_catalog(perturbed_rows)

            for risk in RISKS:
                for role in ROLES:
                    key = f"{risk}/{role}"
                    before = baseline[(risk, role)]
                    after = perturbed[(risk, role)]

                    if before is None and after is None:
                        stats[pert][key]["unresolved"] += 1
                    elif before != after:
                        stats[pert][key]["flips"] += 1

    perturbations: dict[str, dict[str, dict[str, float | int]]] = {}
    for pert in PERTURBATIONS:
        perturbations[pert] = {}
        for risk in RISKS:
            for role in ROLES:
                key = f"{risk}/{role}"
                flips = int(stats[pert][key]["flips"])
                unresolved = int(stats[pert][key]["unresolved"])
                flip_rate = round(flips / catalogs, 4) if catalogs else 0.0
                perturbations[pert][key] = {
                    "flip_rate": flip_rate,
                    "unresolved": unresolved,
                }

    summary = {
        "max_flip_rate_by_perturbation": {
            pert: round(
                max(
                    float(perturbations[pert][f"{risk}/{role}"]["flip_rate"])
                    for risk in RISKS
                    for role in ROLES
                ),
                4,
            )
            for pert in PERTURBATIONS
        }
    }

    return {
        "seed": seed,
        "catalogs": catalogs,
        "perturbations": perturbations,
        "fail_closed": fail_closed,
        "summary": summary,
    }

def stability_seeds(catalogs: int) -> dict[str, Any]:
    seeds = list(STABILITY_SEEDS)
    rates: dict[str, dict[str, float]] = {}
    for seed in STABILITY_SEEDS:
        result = run(catalogs, seed)
        intel_pm1 = result["perturbations"]["intel_pm1"]
        rates[str(seed)] = {
            risk: max(
                float(intel_pm1[f"{risk}/{role}"]["flip_rate"])
                for role in ROLES
            )
            for risk in ("high", "critical")
        }
    return {"seeds": seeds, "intel_pm1_max_flip_rate": rates}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "spawner-sensitivity.json"),
        help="output JSON path",
    )
    parser.add_argument("--catalogs", type=int, default=200)
    args = parser.parse_args(argv)

    if args.catalogs < 1:
        parser.error("--catalogs must be >= 1")

    result = run(args.catalogs)
    result["stability_seeds"] = stability_seeds(args.catalogs)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
