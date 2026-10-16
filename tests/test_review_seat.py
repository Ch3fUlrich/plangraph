"""Seat runner glue: legs, parts, verdicts, keeper compat (hermetic)."""
import json
import sys
from pathlib import Path

import pytest

from plangraph.review import family, gateway, qoder, seat, verdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "review"


def read(name):
    """Read one recorded reply fixture."""
    return (FIX / name).read_text(encoding="utf-8")


def gw_result(text, finish="stop", served="ovh/gpt-oss-120b", status=200,
              error=None):
    """Gateway CallResult with the given reply shape."""
    return gateway.CallResult(
        status=status, text=text, finish_reason=finish,
        served_model=served, requested_model="ovh/gpt-oss-120b",
        correlation_id="corr-1", response_header_names=[],
        tokens={}, prompt_sha256="ab" * 32,
        started="2026-01-01T00:00:00Z", finished="2026-01-01T00:00:01Z",
        error=error, session_tag="review/t", run_id="r")


def qo_result(text, finish="stop"):
    """Qoder QoderResult with the given reply shape."""
    return qoder.QoderResult(
        rc=0, text=text, finish_reason=finish, num_turns=1,
        is_error=False, model="Qwen3.8-Flash", prompt_sha256="ab" * 32,
        started="2026-01-01T00:00:00Z", finished="2026-01-01T00:00:01Z",
        error=None, usage={})


def spec_for(tmp_path, writer, **over):
    """SeatSpec with a tmp out root and one small file."""
    args = {"key": "k", "writer_family": writer, "vectors": ["check X"],
            "files": {"a.txt": "content"}, "out_root": tmp_path / "out"}
    args.update(over)
    return seat.SeatSpec(**args)


def dry_run(seat_dir, tmp_path, writer="anthropic", capsys=None):
    """Run the keeper tool in dry-run mode; return (exit, body)."""
    tok = tmp_path / "tok"
    tok.mkdir(exist_ok=True)
    code = sv.main(["--key", "k", "--seat-dir", str(seat_dir),
                    "--writer-family", writer, "--token-dir", str(tok),
                    "--dry-run"])
    body = None
    if capsys is not None:
        out = capsys.readouterr().out
        body = json.loads(out)["body"] if out.strip() else None
    return code, body


def test_writer_openai_uses_qwen(tmp_path):
    """Writer openai leaves only the qwen (qoder) leg."""
    seen = {}

    def fake_qoder(prompt, model=None):
        seen["model"] = model
        return qo_result(read("ok_pass.txt"))

    def no_gateway(*a, **k):
        raise AssertionError("gateway must not be called")

    outcomes = seat.run_seat(spec_for(tmp_path, "openai"),
                             gateway_call=no_gateway, qoder_run=fake_qoder)
    assert len(outcomes) == 1
    item = outcomes[0]
    assert (item.leg, item.family) == ("qwen", "alibaba")
    assert seen["model"] == "Qwen3.8-Flash"
    assert item.pass_allowed is True
    assert (item.dir / "review.txt").is_file()
    assert (item.dir / "evidence.json").is_file()


def test_writer_alibaba_uses_openai(tmp_path):
    """Writer alibaba leaves only the openai (gateway) leg."""
    def fake_gateway(model, prompt, title=None):
        assert model == "ovh/gpt-oss-120b"
        return gw_result(read("ok_pass.txt"))

    def no_qoder(*a, **k):
        raise AssertionError("qoder must not be called")

    outcomes = seat.run_seat(spec_for(tmp_path, "alibaba"),
                             gateway_call=fake_gateway, qoder_run=no_qoder)
    assert len(outcomes) == 1
    assert (outcomes[0].leg, outcomes[0].family) == ("openai", "openai")
    assert outcomes[0].pass_allowed is True


def test_need_two_anthropic_gives_two_families(tmp_path):
    """Writer anthropic with need=2 fans out to both legs."""
    def fake_gateway(model, prompt, title=None):
        return gw_result(read("ok_pass.txt"))

    def fake_qoder(prompt, model=None):
        return qo_result(read("ok_pass.txt"))

    outcomes = seat.run_seat(spec_for(tmp_path, "anthropic", need=2),
                             gateway_call=fake_gateway, qoder_run=fake_qoder)
    assert sorted(o.leg for o in outcomes) == ["openai", "qwen"]
    assert sorted(o.family for o in outcomes) == ["alibaba", "openai"]


def test_down_qwen_with_openai_raises(tmp_path):
    """Writer openai with qwen down has no eligible leg."""
    with pytest.raises(family.NoEligibleLeg):
        seat.run_seat(spec_for(tmp_path, "openai", down=frozenset({"qwen"})),
                       gateway_call=lambda *a, **k: None,
                       qoder_run=lambda *a, **k: None)


def test_missing_key_propagates(tmp_path):
    """Config errors are never collected as failed legs."""
    def bad_gateway(model, prompt, title=None):
        raise gateway.MissingKey("missing review key: set X")

    with pytest.raises(gateway.MissingKey):
        seat.run_seat(spec_for(tmp_path, "alibaba"),
                       gateway_call=bad_gateway,
                       qoder_run=lambda *a, **k: None)


def test_failed_leg_collected(tmp_path):
    """A non-200 gateway reply collects with verdict None, no raise."""
    def fake_gateway(model, prompt, title=None):
        return gw_result("", finish=None, status=500, error="HTTP 500: boom")

    outcomes = seat.run_seat(spec_for(tmp_path, "alibaba"),
                             gateway_call=fake_gateway,
                             qoder_run=lambda *a, **k: None)
    assert len(outcomes) == 1
    item = outcomes[0]
    assert item.verdict.verdict is None
    assert item.pass_allowed is False
    assert item.error
    assert seat.overall(outcomes) == {"openai": "incomplete"}


def test_pass_compat_dry_run(tmp_path, capsys):
    """Passing seat dirs send ok True through the keeper tool."""
    def fake_gateway(model, prompt, title=None):
        return gw_result(read("ok_pass.txt"))

    outcomes = seat.run_seat(spec_for(tmp_path, "alibaba"),
                             gateway_call=fake_gateway,
                             qoder_run=lambda *a, **k: None)
    code, body = dry_run(outcomes[0].dir, tmp_path, capsys=capsys)
    assert code == 0
    assert body["ok"] is True


def test_bold_verdict_still_passes(tmp_path, capsys):
    """Bold-wrapped verdict lines still pass the keeper gate."""
    def fake_qoder(prompt, model=None):
        return qo_result(read("bold_verdict.txt"))

    outcomes = seat.run_seat(spec_for(tmp_path, "openai"),
                             gateway_call=lambda *a, **k: None,
                             qoder_run=fake_qoder)
    assert outcomes[0].pass_allowed is True
    code, body = dry_run(outcomes[0].dir, tmp_path, capsys=capsys)
    assert code == 0
    assert body["ok"] is True


def test_gaming_missing_refused(tmp_path, capsys):
    """A no without the hit count never passes; keeper exits 4."""
    def fake_gateway(model, prompt, title=None):
        return gw_result(read("no_without_zero_hits.txt"))

    outcomes = seat.run_seat(spec_for(tmp_path, "alibaba"),
                             gateway_call=fake_gateway,
                             qoder_run=lambda *a, **k: None)
    assert outcomes[0].pass_allowed is False
    code, _ = dry_run(outcomes[0].dir, tmp_path, capsys=capsys)
    assert code == 4


def test_length_empty_not_zero(tmp_path, capsys):
    """Truncated empty replies never send as pass."""
    def fake_gateway(model, prompt, title=None):
        return gw_result("", finish="length")

    outcomes = seat.run_seat(spec_for(tmp_path, "alibaba"),
                             gateway_call=fake_gateway,
                             qoder_run=lambda *a, **k: None)
    assert outcomes[0].pass_allowed is False
    code, _ = dry_run(outcomes[0].dir, tmp_path, capsys=capsys)
    assert code in (3, 4) and code != 0


def test_qoder_tool_call_never_passes(tmp_path, capsys):
    """Tool-call finishes never pass even with a pass verdict inside."""
    def fake_qoder(prompt, model=None):
        return qo_result(read("ok_pass.txt"), finish="tool_call")

    outcomes = seat.run_seat(spec_for(tmp_path, "openai"),
                             gateway_call=lambda *a, **k: None,
                             qoder_run=fake_qoder)
    assert outcomes[0].verdict.verdict is None
    assert outcomes[0].pass_allowed is False
    assert outcomes[0].error
    code, _ = dry_run(outcomes[0].dir, tmp_path, capsys=capsys)
    assert code != 0


def test_fix_sent_ok_false(tmp_path, capsys):
    """Fix verdicts send with ok False and exit 0."""
    def fake_gateway(model, prompt, title=None):
        return gw_result(read("fix_with_findings.txt"))

    outcomes = seat.run_seat(spec_for(tmp_path, "alibaba"),
                             gateway_call=fake_gateway,
                             qoder_run=lambda *a, **k: None)
    assert outcomes[0].verdict.verdict == "fix"
    assert seat.overall(outcomes) == {"openai": "fix"}
    code, body = dry_run(outcomes[0].dir, tmp_path, capsys=capsys)
    assert code == 0
    assert body["ok"] is False


def test_qwen_served_model_maps_to_alibaba(tmp_path):
    """The qwen seat dir serves the qoder model the keeper gates on."""
    def fake_qoder(prompt, model=None):
        return qo_result(read("ok_pass.txt"))

    outcomes = seat.run_seat(spec_for(tmp_path, "openai"),
                             gateway_call=lambda *a, **k: None,
                             qoder_run=fake_qoder)
    evidence = json.loads((outcomes[0].dir / "evidence.json").read_text())
    assert evidence["served_model"] == "Qwen3.8-Flash"
    assert sv.model_family(evidence["served_model"]) == "alibaba"


def test_multipart_fix_aggregates(tmp_path):
    """Two parts with one fix aggregate to fix for that leg."""
    files = {"a.txt": "a" * 600, "b.txt": "b" * 600}
    calls = []

    def fake_qoder(prompt, model=None):
        calls.append(prompt)
        if len(calls) == 1:
            return qo_result(read("ok_pass.txt"))
        return qo_result(read("fix_with_findings.txt"))

    spec = spec_for(tmp_path, "openai", files=files, limit=1000)
    outcomes = seat.run_seat(spec, gateway_call=lambda *a, **k: None,
                             qoder_run=fake_qoder)
    assert len(outcomes) == 2
    assert len(calls) == 2
    assert sorted(o.dir.name for o in outcomes) == ["qwen-p1", "qwen-p2"]
    assert seat.overall(outcomes) == {"qwen": "fix"}


def test_overall_states():
    """Halt wins over fix; all-pass passes; bare fail is incomplete."""
    def item(leg, word, allowed):
        return seat.SeatOutcome(leg=leg, family="f", dir=Path("."),
                                verdict=verdict.judge(
                                    "VERDICT: %s\nTEST-GAMING: no - searched s: 0 hits\n" % word,
                                    "stop", material="s: 0 hits")
                                if word else verdict.Verdict(
                                    verdict=None, finish_reason="stop",
                                    test_gaming="missing",
                                    test_gaming_quote=None, empty=False),
                                pass_allowed=allowed, error=None)

    assert seat.overall([item("a", "pass", True)]) == {"a": "pass"}
    assert seat.overall([item("a", "fix", False)]) == {"a": "fix"}
    assert seat.overall([item("a", "halt", False)]) == {"a": "halt"}
    mixed = [item("a", "fix", False), item("a", "halt", False)]
    assert seat.overall(mixed) == {"a": "halt"}
    assert seat.overall([item("a", "", False)]) == {"a": "incomplete"}
