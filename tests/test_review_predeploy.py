"""RT-P1-REVIEW pre-deploy gaps: risk families, diff split, actions, retry,
opencode leg, recorded replies (hermetic)."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from plangraph.review import (actions, family, gateway, opencode, prompt,
                              qoder, seat, split, verdict)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "review"


def read(name):
    """Read one recorded reply fixture."""
    return (FIX / name).read_text(encoding="utf-8")


# 1. families_for_risk -------------------------------------------------

@pytest.mark.parametrize("risk", ["auth", "secrets", "live-step",
                                  "public-release", "high", "AUTH"])
def test_high_risk_needs_two_families(risk):
    got = family.families_for_risk(risk, "anthropic")
    assert len(got) == 2 and len(set(got)) == 2 and "anthropic" not in got


def test_standard_risk_one_family():
    assert len(family.families_for_risk("standard", "openai")) == 1
    assert family.families_for_risk(None, "openai")[0] != "openai"


def test_risk_tag_list_any_high():
    assert len(family.families_for_risk(["docs", "secrets"], "openai")) == 2


def test_claude_is_not_banned_as_reviewer():
    assert family.pick_legs("xiaomi", need=3)[2].family == "anthropic"
    assert family.check_cross_family("openai", "claude-opus-4") == "anthropic"
    assert family.pick_legs("openai", down=frozenset({"qwen"}))[0].name == "anthropic"
    with pytest.raises(family.FamilyError):
        family.check_cross_family("anthropic", "claude-opus-4")


def test_risk_unsatisfiable_is_clear_error():
    one = (family.LEGS[0],)
    with pytest.raises(family.NoEligibleLeg) as exc:
        family.families_for_risk("high", "anthropic", legs=one)
    assert "needs 2" in str(exc.value) and "anthropic" in str(exc.value)
    with pytest.raises(family.NoEligibleLeg):
        family.families_for_risk("standard", "openai", legs=one)


def test_families_for_risk_excludes_writer():
    for writer in ("openai", "alibaba", "anthropic", "deepseek"):
        assert writer not in family.families_for_risk("high", writer)


# 2. split_diff_by_module ----------------------------------------------

def _diff(path, body="x"):
    return ("diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n+%s\n"
            % (path, path, path, path, body))


def test_module_of():
    assert split.module_of("README.md") == "."
    assert split.module_of("docs/a/b.md") == "docs"
    assert split.module_of("rust/worker/src/a.rs") == "rust/worker"
    assert split.module_of("src/plangraph/review/x.py") == "src/plangraph"
    assert split.module_of("src/top.py") == "src"


def test_split_groups_by_module_and_maps_files():
    diff = (_diff("rust/worker/a.rs") + _diff("docs/x.md") +
            _diff("rust/worker/b.rs") + _diff("rust/keeper/c.rs"))
    parts = split.split_diff_by_module(diff, 10_000)
    assert [p.module for p in parts] == ["rust/worker", "docs", "rust/keeper"]  # first seen
    assert [p.index for p in parts] == [1, 2, 3]
    mapping = split.file_part_map(parts)
    assert mapping == {"docs/x.md": 2, "rust/keeper/c.rs": 3,
                       "rust/worker/a.rs": 1, "rust/worker/b.rs": 1}
    assert "part 1 [rust/worker]: rust/worker/a.rs, rust/worker/b.rs" in \
        split.render_part_map(parts)


def test_split_never_truncates_and_keeps_big_file_alone():
    big = _diff("rust/worker/big.rs", "y" * 5000)
    diff = _diff("rust/worker/a.rs") + big + _diff("rust/worker/z.rs")
    parts = split.split_diff_by_module(diff, 400)
    assert "".join(p.text for p in parts) == diff
    holder = [p for p in parts if "rust/worker/big.rs" in p.files]
    assert len(holder) == 1 and holder[0].files == ["rust/worker/big.rs"]
    assert holder[0].size > 400
    assert all(p.size <= 400 for p in parts if p is not holder[0])


def test_split_packs_a_module_over_several_parts():
    diff = "".join(_diff("docs/f%d.md" % i, "z" * 100) for i in range(6))
    parts = split.split_diff_by_module(diff, 600)
    assert len(parts) > 1 and {p.module for p in parts} == {"docs"}
    assert "".join(p.text for p in parts) == diff


def test_split_empty_and_bad_cap():
    assert split.split_diff_by_module("", 100) == []
    with pytest.raises(ValueError):
        split.split_diff_by_module("x", 0)


def test_assemble_diff_prompts_carry_map():
    diff = _diff("docs/x.md") + _diff("rust/worker/a.rs")
    prompts = prompt.assemble_diff(["check"], diff, 10_000)
    assert len(prompts) == 2
    for text in prompts:
        assert "=== FILE-PART MAP" in text and "docs/x.md" in text
    assert "DIFF PART 2 [rust/worker]" in prompts[1]


# 3. plan_actions ------------------------------------------------------

def test_plan_fix_one_card_per_finding():
    got = actions.plan_actions({"verdict": "fix", "findings": [
        {"title": "A", "finding": "fa", "files": ["x.py"]},
        {"finding": "fb"}, "plain text"]}, "P1")
    assert got["pass"] is False and got["inbox"] == []
    assert len(got["fix_cards"]) == 3
    assert got["fix_cards"][0] == {"title": "A", "finding": "fa", "files": ["x.py"]}
    assert got["fix_cards"][1]["files"] == []
    assert "P1" in got["fix_cards"][1]["title"]
    assert got["fix_cards"][2]["finding"] == "plain text"


def test_plan_fix_without_findings_uses_reason():
    got = actions.plan_actions({"verdict": "fix", "reason": "bad flush"}, "P2")
    assert len(got["fix_cards"]) == 1
    assert got["fix_cards"][0]["finding"] == "bad flush"


def test_plan_halt_one_inbox_no_default():
    got = actions.plan_actions({"verdict": "halt", "reason": "scope",
                                "risk_tags": ["auth"]}, "P3", key="demo:A")
    assert got["fix_cards"] == [] and got["pass"] is False
    assert len(got["inbox"]) == 1
    item = got["inbox"][0]
    assert item["default_answer"] is None and item["risk_tags"] == ["auth"]
    assert "P3" in item["text"] and "scope" in item["text"]


def test_plan_pass_is_empty():
    assert actions.plan_actions("pass", "P4") == {
        "fix_cards": [], "inbox": [], "pass": True}


def test_plan_accepts_judged_verdict_and_blocks_unallowed_pass():
    judged = verdict.judge(read("ok_pass.txt"), "stop")
    assert actions.plan_actions(judged, "P5")["pass"] is True
    cut = verdict.judge(read("ok_pass.txt"), "length")
    assert actions.plan_actions(cut, "P5")["pass"] is False


def test_plan_unknown_verdict_plans_nothing():
    assert actions.plan_actions({"verdict": None}, "P6") == {
        "fix_cards": [], "inbox": [], "pass": False}


# 4. run_seat retry ----------------------------------------------------

def gw(text, finish="stop", status=200, error=None, model="m"):
    return gateway.CallResult(
        status=status, text=text, finish_reason=finish, served_model=model,
        requested_model=model, correlation_id="c", response_header_names=[],
        tokens={}, prompt_sha256="ab" * 32, started="s", finished="f",
        error=error, session_tag="t", run_id="r")


def qo(text, finish="stop", error=None, limited=False):
    return qoder.QoderResult(
        rc=0, text=text, finish_reason=finish, num_turns=1, is_error=False,
        model="Qwen3.8-Flash", prompt_sha256="ab" * 32, started="s",
        finished="f", error=error, usage={}, rate_limited=limited)


def oc(text, finish="stop"):
    return opencode.OpencodeResult(
        rc=0, text=text, finish_reason=finish, model="opencode/deepseek-v3",
        prompt_sha256="ab" * 32, started="s", finished="f", error=None)


def spec_for(tmp_path, writer, **over):
    args = {"key": "k", "writer_family": writer, "vectors": ["check X"],
            "files": {"a.txt": "content"}, "out_root": tmp_path / "out"}
    args.update(over)
    return seat.SeatSpec(**args)


def never(*a, **k):
    raise AssertionError("must not be called")


@pytest.mark.parametrize("status", [429, 500, 503, 0])
def test_gateway_failure_retries_next_family(tmp_path, status):
    def gateway_call(model, prompt_text, title=None):
        return gw("", finish=None, status=status, error="HTTP %d" % status)

    outcomes = seat.run_seat(
        spec_for(tmp_path, "alibaba"), gateway_call=gateway_call,
        qoder_run=never, opencode_run=lambda p, model=None: oc(read("ok_pass.txt")))
    good = outcomes[-1]
    assert good.pass_allowed and good.leg == "opencode"
    assert good.tried == ["openai", "anthropic", "opencode"]
    assert [o.leg for o in outcomes] == ["openai", "anthropic", "opencode"]


def test_other_4xx_does_not_retry(tmp_path):
    outcomes = seat.run_seat(
        spec_for(tmp_path, "alibaba"),
        gateway_call=lambda m, p, title=None: gw("", None, 401, "HTTP 401"),
        qoder_run=never, opencode_run=never)
    assert len(outcomes) == 1 and outcomes[0].tried == ["openai"]


def test_timeout_exception_retries(tmp_path):
    def slow(prompt_text, model=None):
        raise TimeoutError("slow")

    # qwen is first for writer openai; anthropic answers well
    outcomes = seat.run_seat(
        spec_for(tmp_path, "openai"),
        gateway_call=lambda m, p, title=None: gw(read("ok_pass.txt")),
        qoder_run=slow, opencode_run=never)
    assert [o.leg for o in outcomes] == ["qwen", "anthropic"]
    assert outcomes[0].error.startswith("TimeoutError")
    assert outcomes[1].pass_allowed and outcomes[1].tried == ["qwen", "anthropic"]


def test_qoder_stderr_rate_limit_marks_down(tmp_path):
    outcomes = seat.run_seat(
        spec_for(tmp_path, "openai"),
        gateway_call=lambda m, p, title=None: gw(read("ok_pass.txt")),
        qoder_run=lambda p, model=None: qo("", "error", "rate limit", True),
        opencode_run=never)
    assert outcomes[-1].leg == "anthropic" and outcomes[-1].pass_allowed


@pytest.mark.parametrize("finish", ["length", "empty", "error"])
def test_incomplete_reply_retries(tmp_path, finish):
    calls = []

    def qoder_run(p, model=None):
        calls.append(model)
        return qo(read("length_partial.txt"), finish)

    outcomes = seat.run_seat(
        spec_for(tmp_path, "openai"),
        gateway_call=lambda m, p, title=None: gw(read("ok_pass.txt")),
        qoder_run=qoder_run, opencode_run=never)
    assert outcomes[0].pass_allowed is False
    assert outcomes[-1].leg == "anthropic" and outcomes[-1].pass_allowed


def test_all_legs_fail_returns_every_attempt_without_raising(tmp_path):
    outcomes = seat.run_seat(
        spec_for(tmp_path, "openai"),
        gateway_call=lambda m, p, title=None: gw("", None, 429, "HTTP 429"),
        qoder_run=lambda p, model=None: qo("", "timeout", "t"),
        opencode_run=lambda p, model=None: oc("", "empty"))
    assert [o.leg for o in outcomes] == ["qwen", "anthropic", "opencode"]
    assert not any(o.pass_allowed for o in outcomes)
    assert outcomes[0].tried == ["qwen", "anthropic", "opencode"]


def test_anthropic_429_completes_on_non_claude_leg(tmp_path):
    """No leg hard-requires Claude (D-1533)."""
    seen = []

    def gateway_call(model, prompt_text, title=None):
        seen.append(model)
        if model.startswith("claude"):
            return gw("", None, 429, "HTTP 429: rate limit")
        return gw(read("ok_pass.txt"))

    outcomes = seat.run_seat(
        spec_for(tmp_path, "openai", down=frozenset({"qwen"})),
        gateway_call=gateway_call, qoder_run=never,
        opencode_run=lambda p, model=None: oc(read("ok_pass.txt")))
    assert seen == ["claude-sonnet-4-5"]
    done = outcomes[-1]
    assert done.family != "anthropic" and done.pass_allowed
    assert done.tried == ["anthropic", "opencode"]
    assert seat.overall(outcomes) == {"anthropic": "incomplete", "opencode": "pass"}


def test_need_two_retry_does_not_steal_other_seat(tmp_path):
    outcomes = seat.run_seat(
        spec_for(tmp_path, "xiaomi", need=2),
        gateway_call=lambda m, p, title=None: (
            gw("", None, 429, "x") if m.startswith("ovh/") else gw(read("ok_pass.txt"))),
        qoder_run=lambda p, model=None: qo(read("ok_pass.txt")),
        opencode_run=lambda p, model=None: oc(read("ok_pass.txt")))
    passing = [o for o in outcomes if o.pass_allowed]
    assert len({o.family for o in passing}) == 2
    assert {o.leg for o in passing} == {"qwen", "anthropic"}


def test_run_seat_diff_splits_by_module(tmp_path):
    diff = _diff("docs/x.md") + _diff("rust/worker/a.rs")
    prompts = []

    def qoder_run(p, model=None):
        prompts.append(p)
        return qo(read("ok_pass.txt"))

    outcomes = seat.run_seat(
        spec_for(tmp_path, "openai", diff=diff, files={}),
        gateway_call=never, qoder_run=qoder_run, opencode_run=never)
    assert len(prompts) == 2 and len(outcomes) == 2
    assert sorted(o.dir.name for o in outcomes) == ["qwen-p1", "qwen-p2"]


# 5. opencode leg ------------------------------------------------------

def fake_bin(tmp_path, body):
    """Executable script written via temp name + rename (no ETXTBSY)."""
    tmp = tmp_path / "fake.tmp"
    tmp.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)
    final = tmp_path / "fake-opencode"
    os.replace(tmp, final)
    return str(final)


def test_opencode_argv_attach_only_with_url():
    base = opencode.argv("P", "prov/model")
    assert base == ["opencode", "run", "-m", "prov/model", "--", "P"]
    url = "http://127.0.0.1:4096"
    assert opencode.argv("P", "prov/model", url) == [
        "opencode", "run", "-m", "prov/model", "--attach", url, "--", "P"]


def test_opencode_run_passes_env_url_and_returns_stdout(tmp_path):
    exe = fake_bin(tmp_path, 'echo "$@" > "$PWD/../argv.txt" 2>/dev/null; '
                             'echo "ARGS: $@"; echo "VERDICT: pass"\n')
    env = {opencode.SERVER_ENV_VAR: "http://127.0.0.1:4096"}
    res = opencode.run("hello", model="p/m", binary=exe, env=env)
    assert res.finish_reason == "stop" and res.error is None
    assert "ARGS: run -m p/m --attach http://127.0.0.1:4096 -- hello" in res.text
    assert verdict.parse_verdict(res.text) == "pass"


def test_opencode_run_no_default_host(tmp_path):
    exe = fake_bin(tmp_path, 'echo "ARGS: $@"\n')
    res = opencode.run("hi", model="p/m", binary=exe, env={})
    assert "--attach" not in res.text


def test_opencode_rate_limit_and_failures(tmp_path):
    limited = fake_bin(tmp_path, 'echo "Error: 429 rate limit" >&2; exit 1\n')
    res = opencode.run("x", binary=limited, env={})
    assert res.rate_limited and res.finish_reason == "error"
    quiet = tmp_path / "q"
    quiet.mkdir()
    empty = fake_bin(quiet, "exit 0\n")
    assert opencode.run("x", binary=empty, env={}).finish_reason == "empty"
    boom = fake_bin(tmp_path, "echo oops >&2; exit 3\n")
    assert opencode.run("x", binary=boom, env={}).error == "exit 3"
    missing = opencode.run("x", binary=str(tmp_path / "nope"), env={})
    assert missing.finish_reason == "error" and missing.error == "binary not found"


def test_opencode_write_seat_dir(tmp_path):
    opencode.write_seat_dir(tmp_path / "o", oc("hi"), extra={"part": 0})
    doc = json.loads((tmp_path / "o" / "evidence.json").read_text())
    assert doc["leg"] == "opencode" and doc["part"] == 0
    assert sv.model_family(doc["served_model"]) == "deepseek"


def test_qoder_run_flags_stderr_rate_limit():
    class Done:
        returncode = 1
        stdout = ""
        stderr = "error: HTTP 429 Too Many Requests"

    res = qoder.run("p", runner=lambda *a, **k: Done())
    assert res.rate_limited and res.error == "rate limit"
    assert qoder.is_rate_limited("Rate limit exceeded")
    assert not qoder.is_rate_limited("fine")


# 6. recorded replies --------------------------------------------------

RECORDED = [
    # name, finish, verdict, gaming, pass_allowed
    ("ok_pass.txt", "stop", "pass", "no", True),
    ("fix_with_findings.txt", "stop", "fix", "no", False),
    ("halt.txt", "stop", "halt", "no", False),
    ("length_partial.txt", "length", "pass", "no", False),
    ("test_gaming_yes.txt", "stop", "fix", "yes_unverified_quote", False),
    ("prose_wrapped_json.txt", "stop", "fix", "no", False),
]


@pytest.mark.parametrize("name,finish,word,gaming,allowed", RECORDED)
def test_recorded_reply_judgement(name, finish, word, gaming, allowed):
    judged = verdict.judge(read(name), finish)
    assert (judged.verdict, judged.test_gaming) == (word, gaming)
    assert judged.pass_allowed is allowed


@pytest.mark.parametrize("name,finish,word,gaming,allowed", RECORDED)
def test_recorded_reply_matches_seat_verdict_parser(name, finish, word, gaming,
                                                    allowed):
    ok, _ = sv.parse_verdict(read(name))
    assert ok is (word == "pass")


def test_gaming_yes_verified_against_material():
    material = "x\nif os.environ.get('PG_FAKE_MIGRATE'): return\n"
    judged = verdict.judge(read("test_gaming_yes.txt"), "stop", material)
    assert judged.test_gaming == "yes"
    assert judged.test_gaming_quote == "if os.environ.get('PG_FAKE_MIGRATE'): return"


def test_prose_wrapped_json_findings_feed_plan_actions():
    text = read("prose_wrapped_json.txt")
    start, end = text.index("```json") + 7, text.index("```\n\nBoth")
    findings = json.loads(text[start:end])["findings"]
    plan = actions.plan_actions({"verdict": verdict.parse_verdict(text),
                                 "findings": findings}, "P7")
    assert [c["files"] for c in plan["fix_cards"]] == [
        ["rust/worker/src/worker.rs"], ["rust/worker/tests/e2e.rs"]]
