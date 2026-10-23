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


def _ctx_fixture(tmp_path):
    """Main + subagent transcripts: duplicate id, one call >200k, two model families."""
    projects = tmp_path / "projects"
    day1 = "2024-01-01T00:00:00Z"
    _write_jsonl(
        projects / "p1" / "main.jsonl",
        [
            # same (message.id, requestId) as the next line: dropped by the
            # daily/weighted path (largest wins), counted by the ctx report
            # (first usage record per message.id wins)
            _assistant("m1", "r1", day1, inp=1, out=1, model="claude-opus-5-5"),
            _assistant("m1", "r1", day1, inp=40000, out=30000, cr=89000, cw=20000,
                       model="claude-opus-5-5"),
            _assistant("m2", "r2", day1, out=10000, cr=10000, model="qwen3.8-max"),
            _assistant("m3", "r3", day1, cr=201000, model="claude-sonnet-5-5"),
            # other day: outside the 2024-01-01 window
            _assistant("m4", "r4", "2024-01-02T00:00:00Z", inp=7, out=3,
                       model="claude-opus-5-5"),
        ],
    )
    _write_jsonl(
        projects / "p1" / "s1" / "subagents" / "a1.jsonl",
        [_assistant("m5", "r5", day1, inp=100, out=40, cr=700, cw=160,
                    model="claude-sonnet-5-5")],
    )
    return projects


def test_ctx_report_fixture_transcripts(tmp_path):
    projects = _ctx_fixture(tmp_path)
    records = metrics.iter_usage(projects, host="box1")
    assert len(records) == 5
    assert {r["host"] for r in records} == {"box1"}
    assert {(r["session"], r["session_class"]) for r in records} == {
        ("main", "main"),
        ("s1", "subagent"),
    }
    # the duplicate (message.id, requestId) kept the larger usage (daily path)
    assert sorted(r["input"] for r in records if r["session"] == "main") == [0, 0, 7, 40000]
    # the ctx path counts the FIRST usage record per message.id instead (inp=1)
    ctx_records = metrics.iter_usage_first(projects, host="box1")
    assert len(ctx_records) == 5
    assert sorted(r["input"] for r in ctx_records if r["session"] == "main") == [0, 0, 1, 7]

    report = metrics.ctx_report(ctx_records, "2024-01-01")
    assert report["day"] == "2024-01-01"
    assert [(g["host"], g["session_class"]) for g in report["groups"]] == [
        ("box1", "main"),
        ("box1", "subagent"),
    ]
    main, sub = report["groups"]
    assert main == {
        "host": "box1",
        "session_class": "main",
        "calls": 3,
        "weighted": 31102.0,
        "share_cache_read": 0.954742,
        "share_cache_write": 0.0,
        "share_output": 0.045253,
        "input_from_cache": 0.999995,
        "ctx_p50": 10000,
        "ctx_p90": 201000,
        "ctx_p99": 201000,
        "share_over_200k": 0.333333,
        "cost_usd": 0.18324,
    }
    assert sub == {
        "host": "box1",
        "session_class": "subagent",
        "calls": 1,
        "weighted": 370.0,
        "share_cache_read": 0.7,
        "share_cache_write": 0.16,
        "share_output": 0.04,
        "input_from_cache": 0.729167,
        "ctx_p50": 960,
        "ctx_p90": 960,
        "ctx_p99": 960,
        "share_over_200k": 0.0,
        "cost_usd": 0.001605,
    }
    # only the requested UTC day is reported
    other = metrics.ctx_report(ctx_records, "2024-01-02")
    assert [g["calls"] for g in other["groups"]] == [1]
    assert other["groups"][0]["weighted"] == 10.0
    assert other["groups"][0]["cost_usd"] == 0.00033
    assert metrics.ctx_report([], "2024-01-01") == {"day": "2024-01-01", "groups": []}


def test_ctx_report_counts_the_first_record_per_message_id():
    """S6: two records, same message.id, first inp=1 then inp=40000 -> inp=1, calls=1."""
    tz = dt.timezone.utc

    def rec(inp):
        return {"host": "box1", "session_class": "main", "session": "s",
                "model": "claude-sonnet-5-5", "message_id": "m1",
                "ts": dt.datetime(2024, 5, 1, 0, 0, tzinfo=tz),
                "input": inp, "output": 0, "cache_read": 0, "cache_write": 0}

    report = metrics.ctx_report([rec(1), rec(40000)], "2024-05-01")
    group = report["groups"][0]
    assert group["calls"] == 1
    assert group["ctx_p50"] == 1                 # the first record, not inp=40000
    assert group["weighted"] == 1.0
    assert group["cost_usd"] == pytest.approx(3e-06)   # 1 sonnet input token


def test_ctx_report_first_duplicate_wins_from_transcripts(tmp_path):
    """Red: the ctx path reads transcripts too, and the first duplicate wins."""
    projects = tmp_path / "projects"
    ts = "2024-05-01T00:00:00Z"
    _write_jsonl(
        projects / "p1" / "main.jsonl",
        [
            _assistant("m1", "r1", ts, inp=1, model="claude-sonnet-5-5"),
            _assistant("m1", "r1", ts, inp=40000, model="claude-sonnet-5-5"),
        ],
    )
    ctx_records = metrics.iter_usage_first(projects, host="box1")
    assert [r["input"] for r in ctx_records] == [1]
    group = metrics.ctx_report(ctx_records, "2024-05-01")["groups"][0]
    assert group["calls"] == 1
    assert group["ctx_p50"] == 1

    # the weighted/daily totals keep their own rule: the largest record wins
    daily = metrics.iter_usage(projects, host="box1")
    assert [r["input"] for r in daily] == [40000]


def test_ctx_report_groups_by_host_and_session_class():
    tz = dt.timezone.utc

    def rec(host, cls, inp, cr):
        return {"host": host, "session_class": cls, "session": "s", "model": "claude-x",
                "ts": dt.datetime(2024, 4, 1, 0, 0, tzinfo=tz),
                "input": inp, "output": 0, "cache_read": cr, "cache_write": 0}

    records = [
        rec("box2", "main", 40, 0),
        rec("box1", "subagent", 10, 20),
        rec("box1", "main", 5, 5),
    ]
    report = metrics.ctx_report(records, "2024-04-01")
    assert report["day"] == "2024-04-01"
    assert [(g["host"], g["session_class"]) for g in report["groups"]] == [
        ("box1", "main"),
        ("box1", "subagent"),
        ("box2", "main"),
    ]
    by_host = {(g["host"], g["session_class"]): g for g in report["groups"]}
    assert by_host[("box1", "main")]["calls"] == 1
    assert by_host[("box2", "main")]["weighted"] == 40.0
    assert by_host[("box1", "subagent")]["ctx_p90"] == 30


def test_is_sidechain_record_is_subagent(tmp_path):
    projects = tmp_path / "projects"
    sidechain = _assistant("m1", "r1", "2024-01-01T00:00:00Z", inp=4)
    sidechain["isSidechain"] = True
    _write_jsonl(projects / "p1" / "side.jsonl", [sidechain])
    _write_jsonl(
        projects / "p1" / "plain.jsonl",
        [_assistant("m2", "r2", "2024-01-01T00:00:00Z", inp=6)],
    )
    records = {r["session"]: r for r in metrics.iter_usage(projects, host="box1")}
    assert records["side"]["session_class"] == "subagent"
    assert records["plain"]["session_class"] == "main"
    assert records["plain"]["host"] == "box1"


def test_session_classes_mapping_file(tmp_path):
    projects = tmp_path / "projects"
    ts = "2024-01-01T00:00:00Z"
    _write_jsonl(projects / "p1" / "main.jsonl",
                 [_assistant("m1", "r1", ts, inp=10, model="claude-x")])
    _write_jsonl(projects / "p1" / "other.jsonl",
                 [_assistant("m2", "r2", ts, inp=11, model="claude-x")])
    _write_jsonl(projects / "p1" / "s2" / "subagents" / "a1.jsonl",
                 [_assistant("m3", "r3", ts, inp=12, model="claude-x")])
    path = tmp_path / "session_classes.json"
    path.write_text(json.dumps({"main": "router", "s2": "judge", "ghost": "worker"}),
                    encoding="utf-8")
    classes = metrics.load_session_classes(path)
    assert classes == {"main": "router", "s2": "judge", "ghost": "worker"}

    records = {r["session"]: r for r in metrics.iter_usage(projects, session_classes=classes)}
    assert records["main"]["session_class"] == "router"
    # a launch record never renames a subagent transcript
    assert records["s2"]["session_class"] == "subagent"
    # unnamed session stays main; never guessed from the session name
    assert records["other"]["session_class"] == "main"

    # the same refinement applies to already-parsed records and is idempotent
    parsed = metrics.iter_usage(projects)
    assert metrics.tag_records(parsed, host="box3", session_classes=classes) is parsed
    assert {r["session"]: (r["session_class"], r["host"]) for r in parsed} == {
        "main": ("router", "box3"),
        "other": ("main", "box3"),
        "s2": ("subagent", "box3"),
    }
    metrics.tag_records(parsed, host="box4", session_classes=classes)
    assert {r["session_class"] for r in parsed} == {"router", "main", "subagent"}
    assert {r["host"] for r in parsed} == {"box4"}


def test_session_classes_file_must_be_an_object(tmp_path):
    path = tmp_path / "session_classes.json"
    path.write_text(json.dumps(["main"]), encoding="utf-8")
    with pytest.raises(ValueError):
        metrics.load_session_classes(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        metrics.load_session_classes(path)


def test_price_table_and_read_multipliers():
    assert metrics.read_multiplier("claude-opus-5-5") == 0.05
    assert metrics.read_multiplier("claude-sonnet-5-5-20260101") == 0.05
    assert metrics.read_multiplier("claude-opus-4-1") == 0.1
    assert metrics.read_multiplier("qwen3.8-max") == 0.1
    assert metrics.read_multiplier("") == 0.1
    assert metrics.price_for("claude-opus-5-5") == (15.0, 75.0, 18.75)
    assert metrics.price_for("claude-sonnet-5-5") == (3.0, 15.0, 3.75)
    assert metrics.price_for("claude-3-5-haiku") == (1.0, 5.0, 1.25)
    assert metrics.price_for("qwen3.8-max") == metrics.DEFAULT_PRICE
    assert metrics.DEFAULT_PRICE == (3.0, 15.0, 3.75)

    opus = {"input": 1_000_000, "output": 0, "cache_read": 1_000_000,
            "cache_write": 0, "model": "claude-opus-5-5"}
    # 15.0 input + 0.05 x 15.0 read
    assert metrics.record_cost(opus) == pytest.approx(15.75)
    other = dict(opus, input=0, cache_read=1_000_000, model="qwen3.8-max")
    # 0.1 x 3.0 default input price
    assert metrics.record_cost(other) == pytest.approx(0.3)


def test_nearest_rank_percentiles():
    assert metrics.nearest_rank([], 0.5) == 0
    tens = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert metrics.nearest_rank(tens, 0.5) == 50
    assert metrics.nearest_rank(tens, 0.9) == 90
    assert metrics.nearest_rank(tens, 0.99) == 100
    assert metrics.nearest_rank([70, 10, 40], 0.5) == 40
    assert metrics.nearest_rank([70, 10, 40], 0.9) == 70


def test_share_over_200k_counts_strictly_more_than_200k():
    ts = dt.datetime(2024, 4, 1, tzinfo=dt.timezone.utc)

    def rec(cr):
        return {"host": "box1", "session_class": "main", "session": "s",
                "model": "claude-x", "ts": ts,
                "input": 0, "output": 0, "cache_read": cr, "cache_write": 0}

    group = metrics.ctx_report([rec(200000), rec(200001)], "2024-04-01")["groups"][0]
    assert group["calls"] == 2
    assert group["share_over_200k"] == 0.5
    assert group["ctx_p50"] == 200000


def test_daily_metrics_counts_only_the_requested_day():
    import datetime as dt
    def rec(day, n):
        return {"ts": dt.datetime.fromisoformat(day + "T12:00:00+00:00"), "session": "s",
                "model": "m", "input": n, "output": 0, "cache_read": 0, "cache_write": 0}
    records = [rec("2024-07-31", 1000), rec("2024-08-01", 70), rec("2024-08-02", 5000)]
    rows = {r["metric"]: r["value"] for r in
            metrics.daily_metrics("2024-08-01", records, {"2024-08-01": 1}, 490.0)}
    assert rows["claude_weighted_tokens"] == 70.0
    assert rows["claude_burn_index"] == 1.0          # 70 / (490 / 7)
    assert rows["weighted_tokens_per_landed"] == 70.0
    assert metrics.daily_metrics("2024-08-03", records, {}, None)[0]["value"] == 0.0
