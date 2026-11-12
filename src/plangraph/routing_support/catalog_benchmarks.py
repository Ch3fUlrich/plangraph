"""D-5173 benchmark provenance migration and published snapshot data."""
import json
from pathlib import Path


def migrate(conn):
    """Add nullable provenance without inventing citations for existing rows."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(catalog_benchmarks)")}
    for column in ("source_url", "source_date"):
        if column not in columns:
            conn.execute(f"ALTER TABLE catalog_benchmarks ADD COLUMN {column} TEXT")


def published_rows():
    """Live-source snapshot; absent metrics are intentionally not filled."""
    path = Path(__file__).with_name("catalog_benchmarks.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    # Routing envelope defaults, not a new measurement or effort interpolation.
    for base in ("omniroute/cx-gpt-6.1-sol", "omniroute/cx-gpt-6-luna"):
        medium = next((r for r in rows if r["model_id"] == base + "-medium"), None)
        if medium:
            rows.append(dict(medium, model_id=base))
    return rows


def model_rows(rows):
    """Register measured effort ids without changing routes or operator policy."""
    from plangraph.planner.legs import DEFAULT_LEGS, spawnable_legs
    by_id = {r["model_id"]: r for r in rows}
    for leg in spawnable_legs():
        if leg.model in by_id:
            continue
        provider = {"agy": "agy", "claude": "claude"}.get(leg.harness, "cx")
        if leg.model.startswith("console-meta/"):
            provider = "opencode-go"
        active = leg in DEFAULT_LEGS and not leg.disabled
        by_id[leg.model] = dict(
            model_id=leg.model, provider_id=provider, upstream_id=leg.model,
            family=leg.family, parameter_effort=next((e for e in
                ("low", "medium", "high", "xhigh", "max") if leg.model.endswith("-" + e)), None),
            context_tokens=leg.max_request_tokens, max_output_tokens=64000,
            status="active" if active else "gated", available=int(active),
            unavailable_reason=None if active else "explicit benchmark identity only",
            harness=leg.harness if leg.harness != "claude" else None, tool_mode=leg.tool_mode)
    return list(by_id.values())
