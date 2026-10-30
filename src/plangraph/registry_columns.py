"""Registry columns ``harness`` and ``tool_mode`` of ``catalog_models`` (SPAWNER-REGISTRY-ROUTE, D-3198).

The seed rows (``catalog_data.SEED_MODELS``) get both columns from
:func:`tag_seed_rows`; a catalog file gains them through :func:`migrate` and
rows that lack them (older or imported) are filled by :func:`derive`.
"""
from __future__ import annotations

import sqlite3
from typing import Any

# Runner harness per provider (planner/legs.py HARNESSES): the catalog id is
# passed to that harness as --model.  None = no runner harness addresses these
# ids (agy/qodercli take their own native ids, which the routed legs in
# planner/legs.py carry; cxa = Codex ACP; claude -p, gemini and the direct
# deepseek API are not runner harnesses).  A new provider must be registered
# here: the KeyError in tag_seed_rows is deliberate.
PROVIDER_HARNESS: dict[str, Any] = {
    "opencode-go": "opencode",
    "mimo": "opencode",
    "cx": "opencode",
    "agy": None,
    "qoder": None,
    "cxa": None,
    "claude": None,
    "gemini": None,
    "deepseek": None,
}

# Review legs that give no verdict in tool mode: the spawner inlines the diff
# (D-3134, D-3173).
INLINE_ONLY_MODELS = frozenset({
    "omniroute/cx-gpt-6-luna",
    "cxa/gpt-6-luna-high",
    "mimo/mimo-v2.6-pro",
})

#: Columns added after the first schema; CREATE TABLE IF NOT EXISTS leaves an
#: existing table as it was, so an older catalog file gains them in migrate().
ADDED_COLUMNS = (
    ("catalog_models", "harness",
     "TEXT CHECK (harness IS NULL OR harness IN ('qodercli', 'opencode', 'agy'))"),
    ("catalog_models", "tool_mode",
     "TEXT CHECK (tool_mode IS NULL OR tool_mode IN ('tools', 'inline-only'))"),
)


def tag_seed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["harness"] = PROVIDER_HARNESS[row["provider_id"]]
        row["tool_mode"] = "inline-only" if row["model_id"] in INLINE_ONLY_MODELS else "tools"
    return rows


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def derive(conn: sqlite3.Connection) -> None:
    """Fill harness/tool_mode on rows that lack them (older or imported rows).

    Only NULL cells are filled: a value set in the catalog (an operator edit) stays.
    """
    for provider, harness in PROVIDER_HARNESS.items():
        if harness is not None:
            conn.execute("UPDATE catalog_models SET harness = ? "
                         "WHERE provider_id = ? AND harness IS NULL", (harness, provider))
    for model in sorted(INLINE_ONLY_MODELS):
        conn.execute("UPDATE catalog_models SET tool_mode = 'inline-only' "
                     "WHERE model_id = ? AND tool_mode IS NULL", (model,))
    conn.execute("UPDATE catalog_models SET tool_mode = 'tools' WHERE tool_mode IS NULL")
