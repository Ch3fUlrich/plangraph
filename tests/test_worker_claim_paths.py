"""Claim validation must also cover stored cards that bypassed put_card."""
import pytest

from plangraph.keeper.errors import Refused
from plangraph.planner.worker_claim import build_claim


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
