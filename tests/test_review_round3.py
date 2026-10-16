"""RT-P1-REVIEW round-3 seat fixes: qoder rc, down-leg fallback, risk tags, actions input, opencode inputs."""
import subprocess
import sys

import pytest

from plangraph.review import actions, family, gateway, opencode, qoder, seat


# 1 qoder ---------------------------------------------------------------
class _Done:
    def __init__(self, rc, out, err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


GOOD = '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"partial VERDICT: pass"}'


def test_qoder_nonzero_rc_without_phrase_is_incomplete():
    res = qoder.run("p", runner=lambda *a, **k: _Done(2, GOOD, "segfault"))
    assert res.finish_reason == "error" and not res.rate_limited and res.error == "exit 2"


def test_qoder_zero_rc_still_stops():
    res = qoder.run("p", runner=lambda *a, **k: _Done(0, GOOD))
    assert res.finish_reason == "stop" and res.error is None


@pytest.mark.parametrize("text", ["HTTP/1.1 429 Too Many Requests", "HTTP/2 429", "HTTP/1.1 429", "status: 429"])
def test_qoder_status_line_429(text):
    assert qoder.is_rate_limited(text) and opencode.is_rate_limited(text)


@pytest.mark.parametrize("text", ["listening on :429", "http://h:429/x", "429 files"])
def test_qoder_bare_429_negative(text):
    assert not qoder.is_rate_limited(text)


# 2 seat ----------------------------------------------------------------
def _gw(status=200, text="x\nVERDICT: pass\n"):
    return gateway.CallResult(
        status=status, text=text, finish_reason="stop", served_model="m", requested_model="m",
        correlation_id="c", response_header_names=[], tokens={}, prompt_sha256="ab" * 32,
        started="s", finished="f", error=None, session_tag="t", run_id="r")


def test_down_leg_not_rerun_when_no_replacement(tmp_path, monkeypatch):
    # Two parts; part 1 puts the only leg down, part 2 must NOT re-run it.
    only = (family.Leg("openai", "gateway", "ovh/gpt-oss-120b", "openai"),)
    monkeypatch.setattr(family, "LEGS", only)
    monkeypatch.setattr(seat.prompt, "assemble_all", lambda v, f, lim: ["p1", "p2"])
    calls = []

    def gw(model, text, title=None):
        calls.append(text)
        return _gw(429)

    spec = seat.SeatSpec(key="k", writer_family="anthropic", vectors=["v"], files={"a": "b"},
                         out_root=tmp_path / "o")
    out = seat.run_seat(spec, gateway_call=gw)
    assert calls == ["p1"]
    assert len(out) == 2 and not any(o.pass_allowed for o in out)
    assert seat.overall(out) == {"openai": "incomplete"}


# 3 risk tags -----------------------------------------------------------
@pytest.mark.parametrize("tag", ["High-Risk", "live step", "live_step", " AUTH ", "money-transfer",
                                 "Public Release", "critical", "secrets"])
def test_high_risk_normalised(tag):
    assert family.is_high_risk(tag)
    assert len(family.families_for_risk(tag, "anthropic")) == 2


@pytest.mark.parametrize("tag", ["docs", "standard", "", None])
def test_standard_not_high(tag):
    assert not family.is_high_risk(tag)
    assert len(family.families_for_risk(tag, "anthropic")) == 1


# 4 actions -------------------------------------------------------------
def test_bare_string_files_is_one_file():
    got = actions.plan_actions({"verdict": "fix", "findings": [{"finding": "x", "files": "a/b.py"}]}, "P")
    assert got["fix_cards"][0]["files"] == ["a/b.py"]
    got = actions.plan_actions({"verdict": "fix", "findings": ["x"], "files": "c.py"}, "P")
    assert got["fix_cards"][0]["files"] == ["c.py"]


def test_non_dict_finding_wrapped_not_crash():
    got = actions.plan_actions({"verdict": "fix", "findings": [42, None, ["a"], {"finding": "ok"}]}, "P")
    cards = got["fix_cards"]
    assert len(cards) == 4 and cards[0]["finding"] == "42" and cards[3]["finding"] == "ok"


# 5 opencode ------------------------------------------------------------
@pytest.mark.parametrize("model,url", [
    ("-x", None), ("p/m", "--evil"), ("p/m", "http://u:pw@h:1"), ("p/m", "http://u@h"),
    ("p/m", "file:///etc"), ("p/m", "ftp://h"), ("p/m", "http://")])
def test_opencode_rejects_unsafe(model, url):
    with pytest.raises(ValueError):
        opencode.argv("P", model, url)
    res = opencode.run("P", model=model, binary="/nonexistent",
                       env={opencode.SERVER_ENV_VAR: url} if url else {})
    assert res.finish_reason == "error" and res.error != "binary not found"


def test_opencode_accepts_http_and_https():
    assert "--attach" in opencode.argv("P", "p/m", "https://h.example:4096")
    assert "--attach" in opencode.argv("P", "p/m", "http://127.0.0.1:4096")
