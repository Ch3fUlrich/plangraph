"""Plangraph Spawner Catalog Database Interface.

Provides SQLite-backed persistence, querying, deterministic export/import,
and OmniRoute combo generation for the Plangraph spawner catalog.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence

from .catalog_seed import (
    SEED_BENCHMARKS,
    SEED_CLIENTS,
    SEED_MODELS,
    SEED_PROVIDERS,
    SEED_QUOTA_POOLS,
    SEED_ROUTE_LEGS,
    SEED_ROUTES,
    TIER_RUNG,
)

DEFAULT_DB_PATH: Path = (
    Path(os.environ.get("PLANGRAPH_CATALOG_DB", "~/.cache/plangraph/spawner_catalog.db"))
    .expanduser()
    .resolve()
)

SCHEMA_PATH: Path = Path(__file__).parent / "catalog_schema.sql"


class CatalogDB:
    """SQLite-backed model, provider, quota-pool, and routing catalog."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.is_memory = str(db_path) == ":memory:"
        if self.is_memory:
            self.db_path = ":memory:"
        else:
            self.db_path = str(Path(db_path).resolve())
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            from .private_io import create_private
            self.permissions_secure = create_private(self.db_path)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        if not self.is_memory:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError as exc:
                import logging
                logging.getLogger(__name__).warning('sqlite_wal_failed: %s', type(exc).__name__)
        self.conn.execute("PRAGMA foreign_keys=ON;")
        if not self.is_memory:
            from .private_io import secure_sqlite_files
            self.permissions_secure = secure_sqlite_files(self.db_path) and self.permissions_secure

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self) -> "CatalogDB":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def init_and_seed(self) -> None:
        """Initialize schema and seed catalog tables idempotently."""
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(schema_sql)

        # 1. Quota pools
        self._insert_records("catalog_quota_pools", SEED_QUOTA_POOLS)

        # 2. Providers
        self._insert_records("catalog_providers", SEED_PROVIDERS)

        # 3. Clients
        self._insert_records("catalog_clients", SEED_CLIENTS)

        # 4. Models
        self._insert_records("catalog_models", SEED_MODELS)

        # 5. Benchmarks
        self._insert_records("catalog_benchmarks", SEED_BENCHMARKS)

        # 6. Routes
        self._insert_records("catalog_routes", SEED_ROUTES)

        # 7. Route legs
        self._insert_records("catalog_route_legs", SEED_ROUTE_LEGS)

        self.conn.commit()

    def _insert_records(self, table_name: str, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        cols = list(records[0].keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        sql = f"INSERT OR REPLACE INTO {table_name} ({col_list}) VALUES ({placeholders})"
        cursor = self.conn.cursor()
        for r in records:
            values = [r.get(c) for c in cols]
            cursor.execute(sql, values)
        return len(records)

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        """Fetch model joined with benchmark, provider, and quota pool."""
        sql = """
        SELECT
            m.*,
            COALESCE(qp.tier, p.tier) AS tier,
            p.tier AS provider_tier,
            p.transport AS provider_transport,
            p.gateway_mode AS provider_gateway_mode,
            p.available AS provider_available,
            p.omniroute_id AS provider_omniroute_id,
            qp.tier AS quota_tier,
            qp.window_type,
            qp.pool_burn_multiplier,
            qp.cache_hit_discount,
            b.master_rank,
            b.aa_intelligence,
            b.coding_index,
            b.agentic_index,
            b.terminal_bench,
            b.deepswe,
            b.frontier_code,
            b.scicode,
            b.cyberbench,
            b.verified_tok_per_s,
            b.direct_api_tok_per_s,
            b.alt_provider_tok_per_s,
            b.ttft_seconds,
            b.cost_per_task_usd,
            b.cache_hit_rate,
            b.benchmark_source
        FROM catalog_models m
        JOIN catalog_providers p ON m.provider_id = p.provider_id
        LEFT JOIN catalog_quota_pools qp ON m.quota_pool_id = qp.pool_id
        LEFT JOIN catalog_benchmarks b ON m.model_id = b.model_id
        WHERE m.model_id = ?
        """
        row = self.conn.execute(sql, (model_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d['active'] = d.get('status') == 'active'
        d["id"] = d["model_id"]
        d["context_window"] = d["context_tokens"]
        combos_rows = self.conn.execute(
            "SELECT route_id FROM catalog_route_legs WHERE model_id = ? ORDER BY route_id ASC",
            (model_id,),
        ).fetchall()
        d["combos"] = [r["route_id"] for r in combos_rows]
        return d

    def list_models(
        self,
        active_only: bool = False,
        family: str | None = None,
        exclude_families: Sequence[str] = (),
        min_intelligence: float = 0.0,
        min_context: int = 0,
    ) -> list[dict[str, Any]]:
        """List models filtered and ordered by 5-rung tier ladder, intelligence, and speed."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if active_only:
            where_clauses.append(
                "m.available = 1 AND m.status = 'active' AND p.available = 1 "
                "AND (LOWER(m.model_id) NOT LIKE 'cxa/%' OR LOWER(m.model_id) LIKE 'cxa/gpt-6-luna%')"
            )
        if family:
            where_clauses.append("LOWER(m.family) = ?")
            params.append(family.lower())
        if exclude_families:
            placeholders = ", ".join(["?"] * len(exclude_families))
            where_clauses.append(f"LOWER(m.family) NOT IN ({placeholders})")
            params.extend([f.lower() for f in exclude_families])
        if min_intelligence > 0.0:
            where_clauses.append("b.aa_intelligence >= ?")
            params.append(min_intelligence)
        if min_context > 0:
            where_clauses.append("m.context_tokens >= ?")
            params.append(min_context)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        sql = f"""
        SELECT
            m.*,
            LOWER(COALESCE(qp.tier, p.tier)) AS tier,
            p.tier AS provider_tier,
            p.transport AS provider_transport,
            p.gateway_mode AS provider_gateway_mode,
            p.available AS provider_available,
            p.omniroute_id AS provider_omniroute_id,
            qp.tier AS quota_tier,
            qp.window_type,
            qp.pool_burn_multiplier,
            qp.cache_hit_discount,
            b.master_rank,
            b.aa_intelligence,
            b.coding_index,
            b.agentic_index,
            b.terminal_bench,
            b.deepswe,
            b.frontier_code,
            b.scicode,
            b.cyberbench,
            b.verified_tok_per_s,
            b.direct_api_tok_per_s,
            b.alt_provider_tok_per_s,
            b.ttft_seconds,
            b.cost_per_task_usd,
            b.cache_hit_rate,
            b.benchmark_source
        FROM catalog_models m
        JOIN catalog_providers p ON m.provider_id = p.provider_id
        LEFT JOIN catalog_quota_pools qp ON m.quota_pool_id = qp.pool_id
        LEFT JOIN catalog_benchmarks b ON m.model_id = b.model_id
        {where_sql}
        ORDER BY
            CASE LOWER(COALESCE(qp.tier, p.tier))
                WHEN 'trial' THEN 0
                WHEN 'free' THEN 1
                WHEN 'credit' THEN 2
                WHEN 'subscription' THEN 3
                WHEN 'paid' THEN 4
                ELSE 99
            END ASC,
            b.aa_intelligence DESC,
            b.verified_tok_per_s DESC
        """
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        leg_rows = self.conn.execute(
            "SELECT model_id, route_id FROM catalog_route_legs ORDER BY route_id ASC"
        ).fetchall()
        combos_by_model: dict[str, list[str]] = {}
        for lr in leg_rows:
            combos_by_model.setdefault(lr["model_id"], []).append(lr["route_id"])

        for r in rows:
            r['active'] = r.get('status') == 'active'
            r["id"] = r["model_id"]
            r["context_window"] = r["context_tokens"]
            r["combos"] = combos_by_model.get(r["model_id"], [])

        # Ensure Python-side sorting is strictly in sync with TIER_RUNG
        rows.sort(
            key=lambda r: (
                TIER_RUNG.get(str(r["tier"]).lower(), 99),
                -float(r.get("aa_intelligence") or 0.0),
                -float(r.get("verified_tok_per_s") or 0.0),
            )
        )
        return rows

    def get_route_legs(
        self,
        route_id: str,
        active_only: bool = True,
        exclude_families: Sequence[str] = (),
        min_intelligence: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Get ordered legs for a role route with optional filtering."""
        where_clauses: list[str] = ["rl.route_id = ?"]
        params: list[Any] = [route_id]

        if active_only:
            where_clauses.append(
                "m.available = 1 AND m.status = 'active' AND p.available = 1 "
                "AND (LOWER(m.model_id) NOT LIKE 'cxa/%' OR LOWER(m.model_id) LIKE 'cxa/gpt-6-luna%')"
            )
        if exclude_families:
            placeholders = ", ".join(["?"] * len(exclude_families))
            where_clauses.append(f"LOWER(m.family) NOT IN ({placeholders})")
            params.extend([f.lower() for f in exclude_families])
        if min_intelligence > 0.0:
            where_clauses.append("b.aa_intelligence >= ?")
            params.append(min_intelligence)

        sql = f"""
        SELECT
            rl.route_id,
            rl.position,
            rl.min_risk_band,
            m.*,
            LOWER(COALESCE(qp.tier, p.tier)) AS tier,
            p.tier AS provider_tier,
            p.transport AS provider_transport,
            p.gateway_mode AS provider_gateway_mode,
            p.available AS provider_available,
            p.omniroute_id AS provider_omniroute_id,
            qp.tier AS quota_tier,
            qp.window_type,
            qp.pool_burn_multiplier,
            qp.cache_hit_discount,
            b.master_rank,
            b.aa_intelligence,
            b.coding_index,
            b.agentic_index,
            b.terminal_bench,
            b.deepswe,
            b.frontier_code,
            b.scicode,
            b.cyberbench,
            b.verified_tok_per_s,
            b.direct_api_tok_per_s,
            b.alt_provider_tok_per_s,
            b.ttft_seconds,
            b.cost_per_task_usd,
            b.cache_hit_rate,
            b.benchmark_source
        FROM catalog_route_legs rl
        JOIN catalog_models m ON rl.model_id = m.model_id
        JOIN catalog_providers p ON m.provider_id = p.provider_id
        LEFT JOIN catalog_quota_pools qp ON m.quota_pool_id = qp.pool_id
        LEFT JOIN catalog_benchmarks b ON m.model_id = b.model_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY rl.position ASC
        """
        return [dict(r, active=r['status'] == 'active')
                for r in self.conn.execute(sql, params).fetchall()]

    def export_json(self, output_path: str | Path | None = None) -> dict[str, Any]:
        """Export all catalog tables to a deterministic dictionary / JSON file."""
        tables = [
            ("quota_pools", "SELECT * FROM catalog_quota_pools ORDER BY pool_id ASC"),
            ("providers", "SELECT * FROM catalog_providers ORDER BY provider_id ASC"),
            ("clients", "SELECT * FROM catalog_clients ORDER BY client_id ASC"),
            ("models", "SELECT * FROM catalog_models ORDER BY model_id ASC"),
            ("benchmarks", "SELECT * FROM catalog_benchmarks ORDER BY model_id ASC"),
            ("routes", "SELECT * FROM catalog_routes ORDER BY route_id ASC"),
            ("route_legs", "SELECT * FROM catalog_route_legs ORDER BY route_id ASC, position ASC"),
        ]

        payload: dict[str, Any] = {}
        for key, query in tables:
            rows = [dict(r) for r in self.conn.execute(query).fetchall()]
            payload[key] = rows

        if output_path:
            p = Path(output_path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            from .private_io import atomic_write_private
            atomic_write_private(p, json.dumps(payload, indent=2, sort_keys=True))

        return payload

    def export_combos(self, output_path: str | Path | None = None) -> dict[str, Any]:
        """Render OmniRoute combos configuration for role routes, enforcing tier ladder order."""
        routes = self.conn.execute("SELECT * FROM catalog_routes ORDER BY route_id ASC").fetchall()
        combos_list: list[dict[str, Any]] = []

        for r in routes:
            route_id = r["route_id"]
            min_ctx = r["min_context_tokens"]
            legs = self.get_route_legs(route_id, active_only=True)
            # Enforce paid-last / 5-rung tier ladder ordering on exported combos
            legs.sort(
                key=lambda leg: (
                    1 if str(leg.get("tier") or "").lower() == "paid" else 0,
                    int(leg.get("position") or 0),
                )
            )
            combos_list.append(
                {
                    "name": route_id,
                    "strategy": "priority",
                    "context": "1M" if min_ctx >= 600000 else "128k",
                    "models": [leg["model_id"] for leg in legs],
                }
            )

        result: dict[str, Any] = {
            "$comment": "generated from plangraph spawner catalog_db",
            "combos": combos_list,
        }

        if output_path:
            p = Path(output_path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            from .private_io import atomic_write_private
            atomic_write_private(p, json.dumps(result, indent=2, sort_keys=True))

        return result

    def import_json(self, payload_or_path: dict[str, Any] | str | Path) -> int:
        """Import catalog tables from a dictionary or JSON file."""
        if isinstance(payload_or_path, (str, Path)):
            with open(payload_or_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = payload_or_path

        total_inserted = 0
        table_order = [
            ("quota_pools", "catalog_quota_pools"),
            ("providers", "catalog_providers"),
            ("clients", "catalog_clients"),
            ("models", "catalog_models"),
            ("benchmarks", "catalog_benchmarks"),
            ("routes", "catalog_routes"),
            ("route_legs", "catalog_route_legs"),
        ]

        for payload_key, table_name in table_order:
            records = payload.get(payload_key, [])
            total_inserted += self._insert_records(table_name, records)

        self.conn.commit()
        return total_inserted


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for spawner catalog database management."""
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite catalog DB (default: {DEFAULT_DB_PATH})",
    )

    parser = argparse.ArgumentParser(
        description="Plangraph Spawner Catalog DB",
        parents=[db_parent],
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init-and-seed",
        parents=[db_parent],
        help="Initialize schema and seed database",
    )

    export_json_parser = subparsers.add_parser(
        "export-json",
        parents=[db_parent],
        help="Export catalog tables to JSON",
    )
    export_json_parser.add_argument("--output", "-o", default=None, help="Output JSON path")

    export_combos_parser = subparsers.add_parser(
        "export-combos",
        parents=[db_parent],
        help="Export OmniRoute combos JSON",
    )
    export_combos_parser.add_argument("--output", "-o", default=None, help="Output JSON path")

    import_json_parser = subparsers.add_parser(
        "import-json",
        parents=[db_parent],
        help="Import catalog tables from JSON",
    )
    import_json_parser.add_argument("file", help="Input JSON file path")

    args = parser.parse_args(argv)

    db = CatalogDB(args.db_path)
    try:
        if args.command == "init-and-seed":
            db.init_and_seed()
            print(f"Catalog DB successfully initialized and seeded at: {db.db_path}")
            return 0
        elif args.command == "export-json":
            res = db.export_json(args.output)
            if not args.output:
                print(json.dumps(res, indent=2, sort_keys=True))
            else:
                print(f"Exported catalog tables to: {args.output}")
            return 0
        elif args.command == "export-combos":
            res = db.export_combos(args.output)
            if not args.output:
                print(json.dumps(res, indent=2, sort_keys=True))
            else:
                print(f"Exported OmniRoute combos to: {args.output}")
            return 0
        elif args.command == "import-json":
            count = db.import_json(args.file)
            print(f"Imported {count} records into catalog DB from: {args.file}")
            return 0
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
