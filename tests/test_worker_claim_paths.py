"""Claim validation must also cover stored cards that bypassed put_card."""
import pytest

from plangraph.keeper.errors import Refused
from plangraph.planner.worker_claim import build_claim


@pytest.mark.parametrize("kind", ["typo", None, 42, [], {}])
def test_build_claim_refuses_invalid_check_kind(kind):
    card = {"outcome": "fix", "check_cmd": "true", "write_set": [],
            "check_kind": kind}
    with pytest.raises(Refused) as exc:
        build_claim("demo:A", card, repo="repo", base_sha="abc")
    assert exc.value.code == "validation"


def test_build_claim_protects_check_reads_without_mutating_card():
    card = {"outcome": "fix", "check_cmd": "false", "write_set": [],
            "check_kind": "regression", "check_reads": ["fixtures/input.json"]}
    claim = build_claim("demo:A", card, repo="repo", base_sha="abc")
    assert claim["forbid"] == ["fixtures/input.json"]
    assert "forbid_paths" not in card
    claim["check_reads"].append("other")
    assert card["check_reads"] == ["fixtures/input.json"]


@pytest.mark.parametrize("reads", [["../x"], ["/abs"], [None], None, "tests/a.py", 42])
def test_build_claim_refuses_legacy_bad_check_reads(reads):
    card = {"outcome": "fix", "check_cmd": "true", "write_set": [],
            "check_reads": reads}
    with pytest.raises(Refused) as exc:
        build_claim("demo:A", card, repo="repo", base_sha="abc")
    assert exc.value.code == "validation"


def test_build_claim_keeps_good_check_reads_unchanged():
    reads = ["./tests\\a.py", "fixtures//input.json"]
    card = {"outcome": "fix", "check_cmd": "true", "write_set": [],
            "check_reads": reads}
    claim = build_claim("demo:A", card, repo="repo", base_sha="abc")
    assert claim["check_reads"] == reads
    assert card["check_reads"] == reads


def test_build_claim_does_not_forbid_check_reads_in_write_set():
    card = {"outcome": "fix", "check_cmd": "false",
            "write_set": ["fixtures/input.json"],
            "check_reads": ["fixtures/input.json", "fixtures/readonly.json"]}
    claim = build_claim("demo:A", card, repo="repo", base_sha="abc")
    assert claim["allow"] == ["fixtures/input.json"]
    assert claim["forbid"] == ["fixtures/readonly.json"]
    assert claim["check_reads"] == card["check_reads"]
