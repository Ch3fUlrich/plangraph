"""D-5173 benchmark provenance migration and published snapshot data."""
import json
from pathlib import Path


def migrate(conn):
    """Add nullable provenance without inventing citations for existing rows."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(catalog_benchmarks)")}
    for column in ("source_url", "source_date"):
        if column not in columns:
            conn.execute(f"ALTER TABLE catalog_benchmarks ADD COLUMN {column} TEXT")
    # Component evaluations can be published without an Intelligence Index or
    # cost. Rebuild only the old NOT NULL layout, retaining every column/value.
    info = list(conn.execute("PRAGMA table_info(catalog_benchmarks)"))
    nullable = {"aa_intelligence", "verified_tok_per_s", "cost_per_task_usd"}
    if any(r[3] and r[1] in nullable for r in info):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='catalog_benchmarks'").fetchone()[0]
        for column in nullable:
            sql = sql.replace(column + " REAL NOT NULL", column + " REAL")
        conn.execute("ALTER TABLE catalog_benchmarks RENAME TO catalog_benchmarks_legacy")
        conn.execute(sql)
        names = ", ".join('"' + r[1] + '"' for r in info)
        conn.execute(f"INSERT INTO catalog_benchmarks ({names}) SELECT {names} FROM catalog_benchmarks_legacy")
        conn.execute("DROP TABLE catalog_benchmarks_legacy")


def published_rows():
    """Live-source snapshot; absent metrics are intentionally not filled."""
    path = Path(__file__).with_name("catalog_benchmarks.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    # Routing envelope defaults, not a new measurement or effort interpolation.
    for base in ("omniroute/cx-gpt-6.1-sol", "omniroute/cx-gpt-6-luna"):
        medium = next((r for r in rows if r["model_id"] == base + "-medium"), None)
        if medium:
            rows.append(dict(medium, model_id=base))
    high = next((r for r in rows if r["model_id"] == "agy/gemini-3.8-flash-tiered-high"), None)
    if high:
        rows.append(dict(high, model_id="gemini-3.8-flash"))
    return rows


def model_rows(rows):
    """Register measured effort ids without changing routes or operator policy."""
    from plangraph.planner.legs import spawnable_legs
    by_id = {r["model_id"]: r for r in rows}
    measured = {r["model_id"] for r in published_rows()}
    by_id["claude/claude-haiku-4-5"].update(
        available=0, unavailable_reason="D-5173 unmeasured: no published evaluation")
    for leg in spawnable_legs():
        if leg.model in by_id or leg.model not in measured:
            continue
        provider = {"agy": "agy", "claude": "claude"}.get(leg.harness, "cx")
        if leg.model.startswith("console-meta/"):
            provider = "opencode-go"
        active = not leg.disabled
        by_id[leg.model] = {
            "model_id": leg.model, "provider_id": provider, "upstream_id": leg.model,
            "family": leg.family, "parameter_effort": next((e for e in
                ("low", "medium", "high", "xhigh", "max") if leg.model.endswith("-" + e)), None),
            "context_tokens": leg.max_request_tokens, "max_output_tokens": 64000,
            "status": "active" if active else "gated", "available": int(active),
            "unavailable_reason": None if active else "explicit benchmark identity only",
            "harness": leg.harness if leg.harness != "claude" else None, "tool_mode": leg.tool_mode}
    return list(by_id.values())
