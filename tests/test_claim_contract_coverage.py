"""Behavioral guards for legacy claims and backend-independent validation."""
import pytest

from plangraph.keeper.errors import Refused
from plangraph.planner.worker_claim import build_claim


def test_raw_empty_red_first_coverage():
    card = dict(check_cmd="false", write_set=[], outcome="fix", check_kind="")
    payload = build_claim("demo:C", card, repo="repo", base_sha="abc")
    assert payload["check_kind"] == "change"
    assert payload["check_cmd"] == "false"
    assert payload == build_claim("demo:C", dict(card, check_kind="change"),
                                  repo="repo", base_sha="abc")


def test_parity_is_not_skipped_without_postgres(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("PG_KEEPER_BACKEND", "sqlite")
    for kind in (None, True, 1, [], {}, "typo"):
        with pytest.raises(Refused) as exc:
            build_claim("demo:C", dict(check_cmd="false", write_set=[],
                                      outcome="fix", check_kind=kind),
                        repo="repo", base_sha="abc")
        assert exc.value.code == "validation"
