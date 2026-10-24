import pytest

from plangraph.keeper.errors import Refused
from plangraph.planner.worker_claim import build_claim


@pytest.mark.parametrize("kind", ["typo", "", None, 1])
def test_build_claim_refuses_unknown_check_kind(kind):
    card = {"check_cmd": "true", "write_set": [], "outcome": "o", "check_kind": kind}
    with pytest.raises(Refused) as exc:
        build_claim("demo:A", card, repo="repo", base_sha="base")
    assert exc.value.code == "validation"


@pytest.mark.parametrize("extra,expected", [({}, "change"),
    ({"check_kind": "change"}, "change"), ({"check_kind": "regression"}, "regression")])
def test_build_claim_accepts_known_and_legacy_kinds(extra, expected):
    card = {"check_cmd": "true", "write_set": [], "outcome": "o", **extra}
    assert build_claim("demo:A", card, repo="repo", base_sha="base")["check_kind"] == expected
