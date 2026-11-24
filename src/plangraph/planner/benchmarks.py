"""Benchmark rows for :func:`plangraph.planner.routing.plan_spawn` (split from routing.py)."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from plangraph.routing_errors import SpawnRefused


class BenchmarkError(SpawnRefused):
    """Benchmark policy or storage failure."""


class BenchRequired(BenchmarkError):
    """Strict benchmark policy refused one or more candidate legs."""

    def __init__(self, message, legs=()):
        super().__init__(message)
        self.gaps = [leg for leg in legs if isinstance(leg, dict)]
        self.legs = [leg['leg'] if isinstance(leg, dict) else leg for leg in legs]


class BenchDataError(BenchmarkError):
    """Benchmark storage could not be read; not missing model coverage."""


# Table-specific aliases to the resolver's canonical columns. No fuzzy model aliases.
BENCHMARK_COLUMNS = {
    "catalog_benchmarks": {"model_id": "model_id"},
    "model_benchmarks": {"model_id": "model_id", "intelligence": "aa_intelligence",
                         "tok_per_s": "verified_tok_per_s"},
}


def benchmark_cells(conn, model_id=None):
    """Preferred observation of each model, effort and harness in a current catalog."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='catalog_benchmark_rows'").fetchone() is None:
        return None
    query = 'SELECT * FROM catalog_benchmark_rows'
    params = ()
    if model_id is not None:
        query += ' WHERE model_id = ?'
        params = (model_id,)
    result = {}
    for row in conn.execute(query + ' ORDER BY model_id, effort, harness', params):
        result.setdefault(row['model_id'], []).append(dict(row))
    return result


def _utility_row(cell):
    row = dict(cell, aa_intelligence=cell['score'])
    if row.get('cost_per_task_usd') is None:
        row.pop('cost_per_task_usd', None)
    return row


def benchmark_for_leg(rows, leg):
    qualified = leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}'
    row = rows.get(leg.model, rows.get(qualified, rows.get(leg.id)))
    if row is None or 'benchmark_rows' not in row:
        return row
    cells = row['benchmark_rows']
    effort = leg.effort or ''
    exact = next((cell for cell in cells if cell['effort'] == effort and cell['harness'] == leg.harness), None)
    if exact is None:
        exact = next((cell for cell in cells if cell['legacy'] and
                      (leg.effort is None or cell['effort'] == effort) and
                      cell['harness'] in ('', leg.harness)), None)
    if exact is None or exact['score'] is None:
        return None
    utility = _utility_row(exact)
    return dict(utility, coding_index=row['coding_index']) if row.get('coding_index') is not None else utility


def benchmark_rows_from_db(path):
    """Return (model-id rows, errors) without creating or modifying a database.

    Prefer the current catalog view. Older databases expose raw benchmark tables.
    """
    try:
        uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((t for t in BENCHMARK_COLUMNS if t in tables), None)
            if table is None:
                return {}, [f"{path}: missing benchmark table (catalog_benchmarks or model_benchmarks)"]
            result = {}
            cells = benchmark_cells(conn)
            coding = {}
            if cells is not None:
                if "coding_index" in {c[1] for c in conn.execute("PRAGMA table_info(catalog_benchmarks)")}:
                    # the coding index the view does not carry
                    coding = dict(conn.execute("SELECT model_id, coding_index FROM catalog_benchmarks"))
                for model, values in cells.items():
                    first = next((row for row in values if row['legacy']), values[0])
                    result[model] = dict(_utility_row(first), benchmark_rows=values,
                                         benchmark_row_count=len(values))
                    if coding.get(model) is not None:   # model-level: the cells stay the catalog's own rows
                        result[model]["coding_index"] = coding[model]
                raw_rows = ()
            else:
                raw_rows = conn.execute(f'SELECT * FROM "{table}"')
            for raw in raw_rows:
                row = dict(raw)
                for original, canonical in BENCHMARK_COLUMNS[table].items():
                    if original in row and canonical not in row:
                        row[canonical] = row[original]
                model = row.get("model_id")
                if not isinstance(model, str) or not model:
                    return {}, [f"{path}: {table} row missing model_id"]
                if model in result:
                    result[model]["benchmark_row_count"] += 1
                else:
                    result[model] = dict(row, benchmark_row_count=1)
            # Bare harness models use the exact provider-qualified catalog key.
            # Preserve explicit rows; absent measurements must remain gaps.
            from .legs import DEFAULT_LEGS
            for leg in DEFAULT_LEGS:
                key = leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}'
                if key in result:
                    result.setdefault(leg.model, result[key])
            return result, []
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {}, [f"{path}: cannot read benchmark database: {exc}"]


# --- Rank eligibility rules (plangraph:ROUTE-RANK-RULES) -------------------------------------------

#: A measured row older than this, or measured under another index version, price or served model,
#: no longer speaks for a high or critical card.
STALE_DAYS = 45
#: A new own row needs this many canary cards before it counts for a high or critical card.
CANARY_CARDS = 5
HIGH_RISKS = ("high", "critical")


#: An own measurement replaces the public indices from this many runs on.
OWN_P_HAT_MIN_RUNS = 20
#: Standard deviation charged to an imputed row that carries no standard error.
IMPUTED_DEFAULT_SD = 3.0


def row_quality(model):
    """The quality a row ranks by: own p_hat at n >= 20, else the coding index, else the AA index.

    An imputed row (a value filled in, not measured) ranks at its mean minus one standard deviation,
    so it never outranks a measured row of the same mean.
    """
    from plangraph.routing_support.resolver import _validated_metric
    own = (model.get("origin") == "own" and model.get("score") is not None
           and (model.get("n_runs") or 0) >= OWN_P_HAT_MIN_RUNS)
    if own:
        quality = _validated_metric(model, "score")
    elif model.get("coding_index") is not None:
        quality = _validated_metric(model, "coding_index")
    else:
        quality = _validated_metric(model, "aa_intelligence")
    if model.get("imputed"):
        se = model.get("score_se")
        known = isinstance(se, (int, float)) and not isinstance(se, bool) and se >= 0   # a zero sd is an sd
        quality = max(0.0, quality - (float(se) if known else IMPUTED_DEFAULT_SD))
    return quality


def consistent_scale(rows):
    """Rows ranked together share one quality scale: the coding index counts only when every row has it,
    else all rank by the AA index (a coding index beside an AA index would not be comparable)."""
    rows = list(rows)
    if all(row.get("coding_index") is not None for row in rows):
        return rows
    return [dict(row, coding_index=None) if row.get("coding_index") is not None else row for row in rows]


def _measured_at(row):
    stamp = row.get("measured_at")
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _facts(row):
    """What the row was measured under: its own keys, else the JSON ``source_record``."""
    record = row.get("source_record")
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except ValueError:
            record = None
    record = record if isinstance(record, dict) else {}
    return {name: row.get(name, record.get(name)) for name in ("price", "served_model")}


def current_index_version(rows):
    """The index version of the newest measurement: a row on another version is out of date."""
    dated = [(_measured_at(row), row.get("index_version")) for row in rows.values()
             if isinstance(row, dict) and row.get("index_version")]
    dated = [item for item in dated if item[0] is not None]
    return max(dated, key=lambda item: item[0])[1] if dated else None


def current_facts(leg, catalog_rows, index_version=None):
    """Price, served model and index version now in force for a leg (catalog row, else unknown).

    The catalog publishes the price as ``cost_per_task_usd`` and the served model as ``upstream_id``; a
    measurement recorder that wants the price and served-model rules writes the values it measured under
    as ``{"price": ..., "served_model": ...}`` into the row's ``source_record``."""
    catalog_id = leg.model if "/" in leg.model else f"{leg.harness}/{leg.model}"
    row = next((r for r in catalog_rows if (r.get("id") or r.get("model_id")) in (catalog_id, leg.model)), None) or {}
    return dict(index_version=index_version, price=row.get("price", row.get("cost_per_task_usd")),
                served_model=row.get("served_model", row.get("upstream_id")))


def stale_reason(row, current, now=None):
    """Why a measured row is out of date (age, index_version, price, served_model), or None."""
    now = time.time() if now is None else now
    measured = _measured_at(row)
    if (measured is not None and now - measured > STALE_DAYS * 86400) or (measured is None and row.get("stale")):
        return "age"
    if current.get("index_version") and row.get("index_version") and row["index_version"] != current["index_version"]:
        return "index_version"
    facts = _facts(row)
    for name in ("price", "served_model"):
        if facts[name] is not None and current.get(name) is not None and facts[name] != current[name]:
            return name
    return None


def effort_passes(leg, row, requested):
    """Whether the row's ``effort_channel`` can carry the requested effort unchanged.

    ``none`` carries no effort, ``fixed`` only the effort the row was measured at, ``parameter`` any
    effort the model accepts (an effort the model clamps lower is not carried). An absent or unknown
    channel proves nothing and does not block. An effort is ineligible, never lowered.
    """
    if requested is None:
        return True
    channel = row.get("effort_channel")
    if channel == "none":
        return False
    if channel == "fixed":
        return requested == (row.get("effort") or leg.effort)
    if channel == "parameter":
        from plangraph.routing_support.resolver import clamp_effort
        return clamp_effort(leg.model, requested) == requested
    return True


def rank_ineligible(leg, row, *, risk, effort, current, now=None):
    """``(reason, evidence)`` when the measured row may not rank the leg for this card, else None."""
    if not effort_passes(leg, row, effort):
        reason = ("effort_channel", dict(kind="effort_channel", channel=row.get("effort_channel"), effort=effort))
    elif risk in HIGH_RISKS and (why := stale_reason(row, current, now)):
        reason = ("stale_row", dict(kind="stale_row", why=why, measured_at=row.get("measured_at")))
    elif (risk in HIGH_RISKS and row.get("origin") == "own" and row.get("n_runs") is not None
          and row["n_runs"] < CANARY_CARDS):
        reason = ("canary_pending", dict(kind="canary_pending", n_runs=row["n_runs"], needed=CANARY_CARDS))
    else:
        return None
    logging.getLogger(__name__).info("route_rank_ineligible %s %s", leg.id, json.dumps(reason[1], default=str))
    return reason
