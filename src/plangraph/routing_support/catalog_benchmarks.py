"""D-5173 benchmark provenance migration and published snapshot data."""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote


def blob_url(oid, fragment):
    """Immutable evidence in the public repository, addressed without a site login."""
    return ("https://api.github.com/repositories/1407069640/git/blobs/"
            + oid + "#" + quote(fragment, safe="/"))


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


def snapshot_data():
    """Checked-in observations: live URLs alone cannot freeze a score or effort."""
    path = Path(__file__).with_name("catalog_benchmark_sources.json")
    return json.loads(path.read_text(encoding="utf-8"))


def published_rows():
    """Exact measured identities only; routing aliases belong in the reader."""
    path = Path(__file__).with_name("catalog_benchmarks.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    evidence = path.with_name("catalog_benchmark_sources.json").read_bytes()
    oid = hashlib.sha1(b"blob " + str(len(evidence)).encode() + b"\0" + evidence).hexdigest()
    for row in rows:
        row["source_url"] = blob_url(oid, "published/" + row["model_id"])
    return rows


def benchmark_rows(legacy):
    """Overlay only supplied metrics, preserving legacy ranks and other fields."""
    historical = snapshot_data()["legacy"]
    by_id = {}
    for row in legacy:
        provenance = historical[row["model_id"]]["provenance"]
        by_id[row["model_id"]] = dict(row, source_date=provenance["source_date"],
            source_url=blob_url(provenance["source_oid"], provenance["source_line"]))
    for row in published_rows():
        model = row["model_id"]
        previous = by_id.get(model, {})
        merged = dict(previous, **row)
        if previous:
            merged["benchmark_source"] += "; retained fields: " + previous["source_url"]
        by_id[model] = merged
    return list(by_id.values())


def model_rows(rows):
    """Explicit catalog identities, independent of planner leg policy/providers."""
    path = Path(__file__).with_name("catalog_benchmark_models.json")
    by_id = {r["model_id"]: r for r in rows}
    for row in json.loads(path.read_text(encoding="utf-8")):
        if row["model_id"] in by_id:
            raise ValueError("duplicate benchmark catalog identity: " + row["model_id"])
        by_id[row["model_id"]] = row
    return list(by_id.values())
