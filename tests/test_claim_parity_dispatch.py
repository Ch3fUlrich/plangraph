"""Exercise parity dispatch without requiring a local Postgres service."""
import os
from unittest.mock import MagicMock

import pytest

import test_worker_claim as claims


@pytest.mark.parametrize("postgres", [False, True])
def test_parity_compares_and_visits_backend(monkeypatch, tmp_path, postgres):
    if postgres:
        monkeypatch.setenv("PG_DSN", "test-only-not-a-connection")
    calls = []

    def claim(path, repo, backend, metadata):
        calls.append(backend)
        return backend

    monkeypatch.setattr(claims, "_metadata_claim", claim)
    if postgres or os.environ.get("PG_DSN") or os.environ.get("PG_KEEPER_BACKEND") == "postgres":
        with pytest.raises(AssertionError):
            claims.test_claim_metadata_sqlite_postgres_parity(tmp_path, None, False)
        assert calls == ["sqlite", "postgres"]
    else:
        claims.test_claim_metadata_sqlite_postgres_parity(tmp_path, None, False)
        assert calls == ["sqlite", "sqlite"]


@pytest.mark.parametrize("backend,placeholder", [("sqlite", "?"), ("postgres", "%s")])
def test_metadata_queries_bind_backend_placeholder(monkeypatch, tmp_path, backend, placeholder):
    class Probe:
        def execute(self, query, args):
            assert query.endswith("= " + placeholder)
            assert args == ("demo:C",)
            raise RuntimeError("query reached")

    class World:
        def __init__(self, *args, **kwargs):
            self.store = self
            if backend == "postgres":
                raw = MagicMock()
                raw.broken = raw.closed = False
                raw.cursor.return_value.__enter__.return_value = Probe()
                self.conn = claims.PgConnection(raw, "test")
            else:
                self.conn = Probe()

        def p(self, *args):
            return None

        def create_items(self, *args):
            pass

        def put_card(self, *args):
            pass

        def get_card(self, *args):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(claims, "World", World)
    monkeypatch.setattr(claims, "worker_claim", lambda *a, **kw: (_ for _ in ()).throw(
        claims.Refused("validation", "unknown check_kind")))
    with pytest.raises(RuntimeError, match="query reached"):
        claims._metadata_claim(tmp_path, (tmp_path, "base"), backend, "typo")
