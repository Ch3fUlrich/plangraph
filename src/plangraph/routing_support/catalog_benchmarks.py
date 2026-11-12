"""D-5173 benchmark provenance migration and published snapshot data."""
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
    if "swe_bench_verified" not in columns:
        conn.execute("ALTER TABLE catalog_benchmarks ADD COLUMN swe_bench_verified REAL")
    model_columns = {r[1] for r in conn.execute("PRAGMA table_info(catalog_models)")}
    if "benchmark_only" not in model_columns:
        conn.execute("ALTER TABLE catalog_models ADD COLUMN benchmark_only INTEGER NOT NULL DEFAULT 0")
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
    """Cite the actual model-specific upstream, not our own copy of its claims."""
    path = Path(__file__).with_name("catalog_benchmarks.json")
    return json.loads(path.read_text(encoding="utf-8"))


def benchmark_rows(legacy):
    """Add effort coverage without retuning main's existing scores or routes."""
    historical = snapshot_data()["legacy"]
    by_id = {}
    for row in legacy:
        provenance = historical[row["model_id"]]["provenance"]
        by_id[row["model_id"]] = dict(row, source_date=provenance["source_date"],
            source_url=blob_url(provenance["source_oid"], provenance["source_line"]))
    for row in published_rows():
        model = row["model_id"]
        if model not in by_id:
            by_id[model] = row
    return list(by_id.values())


def model_rows(rows):
    """Explicit catalog identities, independent of planner leg policy/providers."""
    path = Path(__file__).with_name("catalog_benchmark_models.json")
    by_id = {r["model_id"]: r for r in rows}
    for row in json.loads(path.read_text(encoding="utf-8")):
        if row["model_id"] in by_id:
            raise ValueError("duplicate benchmark catalog identity: " + row["model_id"])
        # Catalog visibility is not permission to add an automatic resolver route.
        by_id[row["model_id"]] = dict(row, benchmark_only=1)
    return list(by_id.values())


def backfill_provenance(conn, seeds):
    """Cite unchanged main seeds on upgrade, never relabel operator measurements."""
    for seed in seeds:
        raw = conn.execute("SELECT * FROM catalog_benchmarks WHERE model_id=?",
                           (seed["model_id"],)).fetchone()
        if raw is None:
            continue
        row = dict(raw)
        if row["source_url"] is not None or row["source_date"] is not None:
            continue
        if all(row[key] == value for key, value in seed.items()
               if key not in {"source_url", "source_date"}):
            conn.execute("UPDATE catalog_benchmarks SET source_url=?,source_date=? WHERE model_id=?",
                         (seed["source_url"], seed["source_date"], seed["model_id"]))
