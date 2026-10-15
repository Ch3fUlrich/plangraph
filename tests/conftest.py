"""Make the ``src/`` layout importable without an install step.

With ``PG_KEEPER_BACKEND=postgres`` (the CI ``pytest-postgres`` job) the keeper runs on
Postgres, so tests that open the keeper's SQLite file directly cannot run and are
left out; the contract tests that read the SQLite file are deselected by name (the
frozen suite stays untouched; ``tests/test_keeper_pg_parity.py`` checks the same
chain through the adapter).
"""
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_PG = os.environ.get("PG_KEEPER_BACKEND") == "postgres"

#: tests that read the SQLite file itself (contract: dump/copy ``keeper.db`` via stdlib sqlite3).
_READS_SQLITE_FILE = {
    "tests/test_keeper_brief.py::test_brief_stats_reports_median_and_p90",   # opens the file mode=ro
    "tests/test_keeper_cli.py::test_cli_admin_init_add_principal_verify",    # inspects the db file
    "tests/contract/test_events.py::test_chain_verifies_from_outside",
    "tests/contract/test_events.py::test_event_payload_and_actor_attribution",
    "tests/contract/test_events.py::test_admin_verify_passes_and_flags_tampering",
}


def pytest_ignore_collect(collection_path, config):
    if _PG and collection_path.suffix == ".py" and collection_path.name.startswith("test_"):
        if "contract" not in collection_path.parts:
            try:
                text = collection_path.read_text(encoding="utf-8")
            except OSError:
                return None
            if "sqlite3" in text and "test_keeper_pg_" not in collection_path.name:
                return True
    return None


def pytest_collection_modifyitems(config, items):
    if not _PG:
        return
    kept, dropped = [], []
    for item in items:
        (dropped if item.nodeid in _READS_SQLITE_FILE else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
