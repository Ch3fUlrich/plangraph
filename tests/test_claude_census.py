"""The public Python entry point delegates to real Rust accounting, offline."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def census_binary():
    subprocess.run(["cargo", "build", "-p", "pg-worker-core", "--bin", "pg-usage-census"],
                   cwd=ROOT / "rust", check=True)
    return ROOT / "rust/target/debug/pg-usage-census"


def test_census_fixture_transcript_and_merged_prs(tmp_path, census_binary):
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    rows = [dict(type="custom-title", customTitle="build-L1-0042"),
            dict(type="user", trigger="work"),
            dict(type="assistant", timestamp="2026-11-13T12:00:00Z", message=dict(
                id="request", usage=dict(input_tokens=10, output_tokens=20,
                                         cache_read_input_tokens=100)))]
    (projects / "p/s.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    cache = tmp_path / "prs.json"
    cache.write_text(json.dumps([dict(number=1, mergedAt="2026-11-13T13:00:00Z")]))
    result = subprocess.run(["python3", str(ROOT / "tools/claude_usage.py"), "--census",
                             "--projects", str(projects), "--since", "7d", "--until",
                             "2026-11-14T00:00:00Z", "--pr-cache", str(cache), "--json"],
                            env={**os.environ, "PG_USAGE_CENSUS_BIN": str(census_binary)},
                            capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["roles"]["L1-build"]["weighted_tokens"] == 40
    assert report["roles"]["L1-build"]["turns"] == 1
    assert report["roles"]["L1-build"]["trigger_share"]["work"] == 1
    assert report["roles"]["L1-build"]["weighted_tokens_per_merged_pr"] == 40
    assert report["merged_prs_per_day"]["2026-11-13"] == 1
    assert report["throughput_guard"] == "revert-cut"


def test_census_missing_helper_has_no_fabricated_report(tmp_path):
    result = subprocess.run(["python3", str(ROOT / "tools/claude_usage.py"), "--census", "--json"],
                            env={**os.environ, "PG_USAGE_CENSUS_BIN": str(tmp_path / "absent")},
                            capture_output=True, text=True)
    assert result.returncode == 3
    assert result.stdout == ""
    assert "census unavailable" in result.stderr
