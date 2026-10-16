"""Tests for the pure daily-usage metrics computation."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess

import pytest

from plangraph.governor import metrics


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line if isinstance(line, str) else json.dumps(line))
            f.write("\n")


def _assistant(msg_id, request_id, ts, *, inp=0, out=0, cr=0, cw=0, model="claude-x"):
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": ts,
        "model": model,
        "message": {
            "id": msg_id,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
        },
    }


def test_parse_ts_variants():
    assert metrics.parse_ts("2024-01-02T03:04:05Z") == dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    naive = metrics.parse_ts("2024-01-02T03:04:05")
    assert naive is not None and naive.tzinfo == dt.timezone.utc
    offset = metrics.parse_ts("2024-01-02T03:04:05+02:00")
    assert offset == dt.datetime(2024, 1, 2, 1, 4, 5, tzinfo=dt.timezone.utc)
    assert metrics.parse_ts("nope") is None
    assert metrics.parse_ts(None) is None


def test_dedup_keeps_higher_total(tmp_path):
    projects = tmp_path / "projects"
    low = _assistant("m1", "r1", "2024-01-01T00:00:00Z", inp=1, out=1)
    high = _assistant("m1", "r1", "2024-01-01T00:00:00Z", inp=100, out=100)
    _write_jsonl(projects / "p1" / "s1.jsonl", [low, high, low])
    records = metrics.iter_usage(projects)
    assert len(records) == 1
    assert records[0]["input"] == 100 and records[0]["output"] == 100


def test_weighted_cache_read_discount():
    rec = {"input": 10, "output": 10, "cache_read": 100, "cache_write": 10, "ts": None}
    # 10 + 10 + 10 + 0.1*100 = 40
    assert metrics.weighted(rec) == pytest.approx(40.0)


def test_subagent_glob_and_session(tmp_path):
    projects = tmp_path / "projects"
    _write_jsonl(
        projects / "p1" / "s1" / "subagents" / "a1.jsonl",
        [_assistant("ms", "rs", "2024-02-02T00:00:00Z", inp=5)],
    )
    _write_jsonl(
        projects / "p1" / "main.jsonl",
        [_assistant("mm", "rm", "2024-02-02T00:00:00Z", inp=7)],
    )
    records = {r["session"]: r for r in metrics.iter_usage(projects)}
    assert set(records) == {"s1", "main"}
    assert records["s1"]["input"] == 5


def test_malformed_line_skipped(tmp_path):
    projects = tmp_path / "projects"
    good = _assistant("g", "r", "2024-03-03T00:00:00Z", inp=3)
    bad = '{"usage": "assistant" oops not json'
    non_assistant = {"type": "user", "message": {"usage": {"input_tokens": 99}}}
    _write_jsonl(projects / "p1" / "s1.jsonl", [bad, non_assistant, good])
    records = metrics.iter_usage(projects)
    assert len(records) == 1
    assert records[0]["input"] == 3


def test_daily_weighted_buckets_across_midnight():
    tz = dt.timezone.utc
    before = {"ts": dt.datetime(2024, 5, 1, 23, 59, 59, tzinfo=tz), "input": 10, "output": 0, "cache_read": 0, "cache_write": 0}
    after = {"ts": dt.datetime(2024, 5, 2, 0, 0, 1, tzinfo=tz), "input": 20, "output": 0, "cache_read": 0, "cache_write": 0}
    days = metrics.daily_weighted([before, after])
    assert days == {"2024-05-01": 10.0, "2024-05-02": 20.0}


def test_burn_index_with_and_without_cap():
    assert metrics.burn_index(30.0, 70.0) == pytest.approx(3.0)
    assert metrics.burn_index(30.0, None) is None
    assert metrics.burn_index(30.0, 0) is None
    assert metrics.burn_index(30.0, -5) is None


def test_landed_by_day_parsing_canned_listing():
    canned = (
        "2024-06-02T10:00:00+00:00\n"
        "2024-06-02T23:30:00+00:00\n"
        "2024-06-03T01:00:00+00:00\n"
        "\n"
    )
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        return canned

    counts = metrics.landed_by_day("/repo", since="2024-06-01", until="2024-06-05", runner=runner)
    assert counts == {"2024-06-02": 2, "2024-06-03": 1}
    assert "--merges" in seen["argv"] and "--first-parent" in seen["argv"]
    assert "--since=2024-06-01" in seen["argv"] and "--until=2024-06-05" in seen["argv"]


def test_landed_by_day_without_until_and_failure():
    def runner(argv):
        assert not any(a.startswith("--until=") for a in argv)
        return "2024-07-01T00:00:00+00:00\n"

    assert metrics.landed_by_day("/repo", since="2024-07-01", runner=runner) == {"2024-07-01": 1}

    def failing(_argv):
        raise subprocess.CalledProcessError(128, ["git"])

    assert metrics.landed_by_day("/repo", since="2024-07-01", runner=failing) == {}

    def missing(_argv):
        raise OSError("no git")

    assert metrics.landed_by_day("/repo", since="2024-07-01", runner=missing) == {}


def _by_metric(rows):
    return {r["metric"]: r for r in rows}


def test_daily_metrics_matrix_no_cap_no_landed():
    tz = dt.timezone.utc
    rec = {"ts": dt.datetime(2024, 8, 1, 12, 0, tzinfo=tz), "input": 10, "output": 5, "cache_read": 20, "cache_write": 0}
    rows = metrics.daily_metrics("2024-08-01", [rec], {}, None)
    got = _by_metric(rows)
    assert set(got) == {"claude_weighted_tokens", "landed_changes"}
    # 10 + 5 + 0 + 0.1*20 = 17.0
    assert got["claude_weighted_tokens"]["value"] == pytest.approx(17.0)
    assert got["claude_weighted_tokens"]["source"] == "transcripts"
    assert got["landed_changes"]["value"] == pytest.approx(0.0)
    assert got["landed_changes"]["source"] == "git"


def test_daily_metrics_full_with_cap_and_landed():
    tz = dt.timezone.utc
    rec = {"ts": dt.datetime(2024, 8, 1, 12, 0, tzinfo=tz), "input": 10, "output": 5, "cache_read": 20, "cache_write": 0}
    landed = {"2024-08-01": 2}
    rows = metrics.daily_metrics("2024-08-01", [rec], landed, weekly_cap=119.0)
    got = _by_metric(rows)
    assert set(got) == {
        "claude_weighted_tokens",
        "claude_burn_index",
        "landed_changes",
        "weighted_tokens_per_landed",
    }
    assert got["claude_burn_index"]["source"] == "transcripts"
    assert got["claude_burn_index"]["value"] == pytest.approx(17.0 / (119.0 / 7))
    assert got["weighted_tokens_per_landed"]["source"] == "derived"
    assert got["weighted_tokens_per_landed"]["value"] == pytest.approx(8.5)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_landed_by_day_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)

    git("init")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "Test")
    (repo / "a.txt").write_text("base\n")
    git("add", "a.txt")
    git("commit", "-m", "base")
    for name in ("f1", "f2"):
        git("checkout", "-b", name)
        (repo / f"{name}.txt").write_text(name + "\n")
        git("add", f"{name}.txt")
        git("commit", "-m", name)
        git("checkout", "master" if _has_master(repo) else "main")
        git("merge", "--no-ff", "-m", f"Merge {name}", name)

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    counts = metrics.landed_by_day(repo, since=today + "T00:00:00")
    assert sum(counts.values()) == 2


def _has_master(repo):
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "master"],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0
