"""Tests for the governor CLI and keeper-backed source."""
from __future__ import annotations

import json

import pytest

from plangraph.keeper import cli_verbs
from plangraph.governor import cli, metrics, policy
from plangraph.governor.keeper_source import KeeperSource


class FakeSession:
    """Record calls; serve canned rows keyed by metric name."""

    def __init__(self, values=None, raises=None):
        self.values = values or {}
        self.raises = raises
        self.calls = []

    def call(self, method, verb, params=None, body=None):
        self.calls.append((method, verb, params, body))
        if self.raises is not None:
            raise self.raises
        if verb == "metrics" and method == "GET":
            metric = (params or {}).get("metric")
            if metric in self.values:
                return {"rows": [{"value": self.values[metric]}]}
            return {"rows": []}
        if verb == "governor_override":
            return {"logged": True}
        return {"day": "x", "count": 0}

    def posts(self, verb):
        return [c for c in self.calls if c[0] == "POST" and c[1] == verb]


@pytest.fixture
def stub_daily(monkeypatch, tmp_path):
    """Patch disk-touching metrics calls with canned values."""
    monkeypatch.setattr(metrics, "iter_usage", lambda d: [])
    monkeypatch.setattr(
        metrics, "daily_metrics",
        lambda day, records, landed, cap: [
            {"metric": "claude_burn_index", "value": 2.0, "source": "keeper"}])
    return tmp_path


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_dry_run_prints_items_no_post(stub_daily, capsys):
    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(stub_daily),
                   "--dry-run"], session=s)
    assert rc == 0
    assert s.posts("set_metrics") == []
    out = _out(capsys)
    assert out["posted"] is False
    assert out["items"][0]["metric"] == "claude_burn_index"


def test_daily_posts_once(stub_daily, capsys):
    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(stub_daily)],
                  session=s)
    assert rc == 0
    posts = s.posts("set_metrics")
    assert len(posts) == 1
    assert _out(capsys)["posted"] is True


@pytest.mark.parametrize("failure", ["makedirs", "_write_ctx"])
def test_ctx_write_failure_still_posts(stub_daily, monkeypatch, capsys, failure):
    def fail(*args, **kwargs):
        raise OSError("write failed")
    monkeypatch.setattr(cli.os if failure == "makedirs" else cli, failure, fail)
    session = FakeSession()
    assert cli.main(["metrics", "daily", "--projects-dir", str(stub_daily),
                     "--date", "2024-01-01"], session=session) == 0
    captured = capsys.readouterr()
    assert "cannot write ctx report" in captured.err
    assert json.loads(captured.out)["posted"] is True
    assert len(session.posts("set_metrics")) == 1
    assert not (stub_daily / "ctx-2024-01-01.json").exists()


@pytest.mark.parametrize("fails", [False, True])
def test_write_ctx_unique_atomic_temp_and_cleanup(tmp_path, monkeypatch, fails):
    path = tmp_path / "ctx.json"
    fixed = tmp_path / "ctx.json.tmp"
    fixed.write_text("other writer", encoding="utf-8")
    replace = cli.os.replace
    seen = []

    def inspect(source, destination):
        from pathlib import Path
        source = Path(source)
        seen.append(source)
        assert source.parent == path.parent
        assert source != fixed
        assert not path.exists()
        assert json.loads(source.read_text(encoding="utf-8")) == {"ok": True}
        if fails:
            raise OSError("rename failed")
        replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", inspect)
    if fails:
        with pytest.raises(OSError):
            cli._write_ctx(str(path), {"ok": True})
    else:
        cli._write_ctx(str(path), {"ok": True})
        assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert seen and not seen[0].exists()
    assert fixed.read_text(encoding="utf-8") == "other writer"
    assert path.exists() is not fails


def test_rerun_is_idempotent_body(stub_daily):
    s = FakeSession()
    args = ["metrics", "daily", "--projects-dir", str(stub_daily)]
    assert cli.main(args, session=s) == 0
    assert cli.main(args, session=s) == 0
    posts = s.posts("set_metrics")
    assert len(posts) == 2
    assert posts[0][3] == posts[1][3]


def test_provider_usage_and_rate_limited(stub_daily, capsys, tmp_path):
    usage = tmp_path / "u.json"
    usage.write_text(json.dumps({"qwen": 100}), encoding="utf-8")
    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(stub_daily),
                   "--provider-usage", str(usage),
                   "--rate-limited", "muse"], session=s)
    assert rc == 0
    out = _out(capsys)
    metrics_names = {i["metric"] for i in out["items"]}
    assert "provider_used.qwen" in metrics_names
    assert "rate_limited.muse" in metrics_names


def test_daily_output_carries_ctx_section(tmp_path, capsys):
    """The daily job prints a ctx section; day/items/posted stay as they were."""
    projects = tmp_path / "projects"
    line = {
        "type": "assistant",
        "requestId": "r1",
        "timestamp": "2024-01-01T00:00:00Z",
        "model": "claude-opus-5-5",
        "message": {
            "id": "m1",
            "usage": {
                "input_tokens": 10000,
                "output_tokens": 4000,
                "cache_read_input_tokens": 70000,
                "cache_creation_input_tokens": 16000,
            },
        },
    }
    target = projects / "p1" / "main.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(line) + "\n", encoding="utf-8")
    classes = tmp_path / "session_classes.json"
    classes.write_text(json.dumps({"main": "router"}), encoding="utf-8")

    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(projects),
                   "--date", "2024-01-01", "--host", "box1",
                   "--session-classes", str(classes), "--dry-run"], session=s)
    assert rc == 0
    out = _out(capsys)
    assert out["day"] == "2024-01-01"
    assert out["items"][0]["metric"] == "claude_weighted_tokens"
    assert {i["metric"] for i in out["items"]} == {"claude_weighted_tokens",
                                                   "landed_changes"}
    assert out["posted"] is False
    assert s.posts("set_metrics") == []
    assert out["ctx"]["day"] == "2024-01-01"
    assert out["ctx"]["groups"] == [{
        "host": "box1",
        "session_class": "router",
        "calls": 1,
        "weighted": 37000.0,
        "share_cache_read": 0.7,
        "share_cache_write": 0.16,
        "share_output": 0.04,
        "input_from_cache": 0.729167,
        "ctx_p50": 96000,
        "ctx_p90": 96000,
        "ctx_p99": 96000,
        "share_over_200k": 0.0,
        "cost_usd": 0.8025,
    }]


def _assistant(msg_id, request_id, ts, *, inp=0, out=0, cr=0, cw=0, model="claude-x"):
    """One transcript line shaped like the metrics fixtures'."""
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


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines),
                    encoding="utf-8")


def test_daily_writes_the_ctx_report_file(tmp_path, capsys):
    """The daily job writes the ctx section (per host/class rows) to a file too."""
    projects = tmp_path / "projects"
    day = "2024-01-01"
    ts = f"{day}T00:00:00Z"
    _write_jsonl(projects / "p1" / "main.jsonl", [
        # same (message.id, requestId) as the next line: the ctx file counts the
        # FIRST record (inp=1), the daily items keep the larger one
        _assistant("m1", "r1", ts, inp=1, out=1, model="claude-opus-5-5"),
        _assistant("m1", "r1", ts, inp=40000, out=30000, cr=89000, cw=20000,
                   model="claude-opus-5-5"),
        _assistant("m2", "r2", ts, out=10000, cr=10000, model="qwen3.8-max"),
    ])
    _write_jsonl(
        projects / "p1" / "s1" / "subagents" / "a1.jsonl",
        [_assistant("m5", "r5", ts, inp=100, out=40, cr=700, cw=160,
                    model="claude-sonnet-5-5")],
    )
    out_dir = tmp_path / "governor"

    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(projects),
                   "--date", day, "--host", "box1", "--out-dir", str(out_dir),
                   "--dry-run"], session=s)
    assert rc == 0
    assert s.posts("set_metrics") == []
    path = out_dir / f"ctx-{day}.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["day"] == day
    assert payload["groups"] == [
        {
            "host": "box1",
            "session_class": "main",
            "calls": 2,
            "weighted": 11002.0,
            "share_cache_read": 0.49995,
            "share_cache_write": 0.0,
            "share_output": 0.5,
            "input_from_cache": 0.9999,
            "ctx_p50": 1,
            "ctx_p90": 10000,
            "ctx_p99": 10000,
            "share_over_200k": 0.0,
            "cost_usd": 0.15309,
        },
        {
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
        },
    ]
    # stdout still carries the same ctx section
    assert _out(capsys)["ctx"] == payload


def test_daily_ctx_file_defaults_next_to_the_transcripts(tmp_path, capsys):
    """Without --out-dir the report lands in the projects dir the job reads."""
    projects = tmp_path / "projects"
    day = "2024-01-01"
    _write_jsonl(projects / "p1" / "main.jsonl",
                 [_assistant("m1", "r1", f"{day}T00:00:00Z", inp=7)])

    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(projects),
                   "--date", day, "--host", "box1", "--dry-run"], session=s)
    assert rc == 0
    path = projects / f"ctx-{day}.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["day"] == day
    assert [g["calls"] for g in payload["groups"]] == [1]
    assert _out(capsys)["ctx"] == payload


def test_bad_date(stub_daily):
    s = FakeSession()
    rc = cli.main(["metrics", "daily", "--projects-dir", str(stub_daily),
                   "--date", "not-a-date"], session=s)
    assert rc == 64


def test_missing_projects_dir(stub_daily, monkeypatch):
    monkeypatch.delenv(cli.ENV_PROJECTS, raising=False)
    s = FakeSession()
    assert cli.main(["metrics", "daily"], session=s) == 64


def test_unreachable_is_three(stub_daily):
    s = FakeSession(raises=cli_verbs.Unreachable("down"))
    rc = cli.main(["metrics", "daily", "--projects-dir", str(stub_daily)],
                  session=s)
    assert rc == 3


def test_operator_never_denied(capsys):
    s = FakeSession(values={"claude_burn_index": 5.0})
    rc = cli.main(["check", "planner", "operator"], session=s)
    out = _out(capsys)
    assert out["allow"] is True
    assert rc == 0


def test_agent_planner_burn_denied(capsys):
    s = FakeSession(values={"claude_burn_index": 2.0})
    rc = cli.main(["check", "planner", "agent", "--provider", "claude"],
                  session=s)
    out = _out(capsys)
    assert out["allow"] is False
    assert rc == 1


def test_fallback_allowed_and_logged(capsys):
    s = FakeSession(values={"claude_burn_index": 2.0})
    rc = cli.main(["check", "planner", "agent", "--provider", "claude",
                   "--fallback", "qwen,muse"], session=s)
    out = _out(capsys)
    assert out["allow"] is True
    assert rc == 0
    posts = s.posts("governor_override")
    assert len(posts) == 1
    assert "fallback" in posts[0][3]["reason"]


def test_override_flips_deny_and_logs(capsys):
    s = FakeSession(values={"claude_burn_index": 5.0})
    rc = cli.main(["check", "planner", "agent", "--provider", "claude",
                   "--override", "because"], session=s)
    out = _out(capsys)
    assert out["allow"] is True
    assert rc == 0
    assert len(s.posts("governor_override")) == 1


def test_keeper_source_fails_open():
    boom = FakeSession(raises=cli_verbs.Unreachable("down"))
    src = KeeperSource(boom, today=lambda: "2026-10-15")
    assert src.burn("2026-10-15") is None
    assert src.quota_used("claude", "2026-10-15") is None
    assert src.rate_limited("claude") is None


def test_quota_exhausted_denied(capsys):
    s = FakeSession(values={"provider_used.qwen": 1000.0})
    rc = cli.main(["check", "worker_leg", "agent", "--provider", "qwen",
                   "--quota", "qwen=1"], session=s)
    out = _out(capsys)
    assert out["allow"] is False
    assert rc == 1


def test_rate_limited_claude_planner_falls_back(capsys):
    s = FakeSession(values={
        "claude_burn_index": 0.0, "rate_limited.claude": 1.0})
    rc = cli.main(["check", "planner", "agent", "--provider", "claude",
                   "--fallback", "qwen,muse"], session=s)
    out = _out(capsys)
    assert out["allow"] is True
    assert out["fallback"] is not None
    assert rc == 0
