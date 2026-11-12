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
    return json.loads(path.read_text(encoding="utf-8"))
