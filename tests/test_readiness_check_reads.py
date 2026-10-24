import pytest

from card_path_cases import CHECK_PATH_CASES
from plangraph.planner.readiness import check_card


@pytest.mark.parametrize("path,valid", CHECK_PATH_CASES)
def test_readiness_checks_check_reads_like_forbid_paths(tmp_path, path, valid):
    verdict = check_card({"outcome": "o", "check_cmd": "true", "write_set": [],
                          "check_reads": [path]}, repo_root=tmp_path,
                         dry_run=lambda *_: (True, ""))
    findings = [f for f in verdict.findings if f.code == "bad_path"]
    assert [f.message for f in findings] == ([] if valid else [f"check_reads: {path}"])


@pytest.mark.parametrize("path", ["~", "~/tests/a.py", None, 42])
def test_readiness_keeps_extra_check_read_rejections(tmp_path, path):
    verdict = check_card({"check_cmd": "true", "check_reads": [path]},
                         repo_root=tmp_path, dry_run=lambda *_: (True, ""))
    assert any(f.code == "bad_path" and f.message == f"check_reads: {path}"
               for f in verdict.findings)


@pytest.mark.parametrize("field", ["write_set", "read_set", "forbid_paths"])
@pytest.mark.parametrize("path,invalid", [(".", False), ("./", False), ("~", True)])
def test_readiness_preserves_legacy_path_rules(tmp_path, field, path, invalid):
    verdict = check_card({"check_cmd": "true", field: [path]}, repo_root=tmp_path,
                         dry_run=lambda *_: (True, ""))
    assert any(f.code == "bad_path" for f in verdict.findings) == invalid
