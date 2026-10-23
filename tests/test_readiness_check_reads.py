from plangraph.planner.readiness import check_card


def test_readiness_checks_check_reads_like_forbid_paths(tmp_path):
    verdict = check_card({"outcome": "o", "check_cmd": "true", "write_set": [],
                          "check_reads": ["../escape"]}, repo_root=tmp_path,
                         dry_run=lambda *_: (True, ""))
    assert any(f.code == "bad_path" and "check_reads" in f.message
               for f in verdict.findings)
