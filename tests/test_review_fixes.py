"""W-F3 review fixes: keeper-shaped halt payload, 429 false positives, byte-exact diff
split, per-part seat retry, `--` before the opencode prompt (hermetic)."""
import inspect
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_predeploy import _diff, gw, oc, qo, read, spec_for  # noqa: E402

from plangraph.inbox import routes  # noqa: E402
from plangraph.review import actions, opencode, qoder, seat, split  # noqa: E402

# R-1 --------------------------------------------------------------------


def test_halt_payload_matches_the_keeper_ask_contract():
    got = actions.plan_actions({"verdict": "halt", "reason": "scope", "risk_tags": ["auth"]},
                               "P3", key="demo:A")
    item = got["inbox"][0]
    found = re.findall(r'body\.get\("(\w+)"|_need\(body, "(\w+)"\)',
                       inspect.getsource(routes._ask))
    keys = {a or b for a, b in found}
    assert set(item) == keys == {"key", "text", "default_answer", "deadline", "risk_tags"}
    assert item["key"] == "demo:A" and item["default_answer"] is None
    assert item["deadline"] is None and item["risk_tags"] == ["auth"]


# R-2 --------------------------------------------------------------------

LIMITED = ["HTTP 429", "error: status 429 from upstream", "429 Too Many Requests",
           "Rate limit exceeded", "rate_limit_error", "too many requests, slow down"]
NOT_LIMITED = ["listening on port 4290", "connect 127.0.0.1:429 refused", "processed 429 files",
               "retry in 429 ms", "fine", ""]


@pytest.mark.parametrize("text", LIMITED)
def test_rate_limit_needs_context_positive(text):
    assert qoder.is_rate_limited(text) and opencode.is_rate_limited(text)


@pytest.mark.parametrize("text", NOT_LIMITED)
def test_bare_429_is_not_a_rate_limit(text):
    assert not qoder.is_rate_limited(text) and not opencode.is_rate_limited(text)


def test_opencode_rate_limit_only_on_nonzero_exit():
    class Done:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "VERDICT: pass\n", "HTTP 429 retried"

    ok = opencode.run("p", runner=lambda *a, **k: Done(0), env={})
    assert not ok.rate_limited and ok.finish_reason == "stop"
    bad = opencode.run("p", runner=lambda *a, **k: Done(1), env={})
    assert bad.rate_limited


# R-3 --------------------------------------------------------------------

QUOTED = ('diff --git "a/docs/d\\303\\251 x.txt" "b/docs/d\\303\\251 x.txt"\n'
          '--- "a/docs/d\\303\\251 x.txt"\n+++ "b/docs/d\\303\\251 x.txt"\n@@ -1 +1 @@\n+q\n')
RENAME = ("diff --git a/old.py b/rust/worker/new.py\nsimilarity index 100%\n"
          "rename from old.py\nrename to rust/worker/new.py\n")
BINARY = ("diff --git a/docs/i.bin b/docs/i.bin\nindex 1..2 100644\n"
          "Binary files a/docs/i.bin and b/docs/i.bin differ\n")
UNICODE = _diff("docs/über.md", "ä \x0c line sep")
BIG = _diff("src/plangraph/big.py", "y" * 5000)
CHUNK = re.compile(r"(?m)^diff --git .*\n(?:(?!diff --git ).*\n)*")


@pytest.mark.parametrize("cap", [10, 300, 100_000])
def test_parts_reproduce_the_diff_byte_for_byte(cap):
    text = _diff("docs/a.md") + QUOTED + BINARY + UNICODE + RENAME + BIG
    parts = split.split_diff_by_module(text, cap)
    assert "".join(p.text for p in parts) == text
    assert sum(p.size for p in parts) == len(text.encode("utf-8"))


def test_interleaved_modules_lose_nothing():
    text = _diff("docs/a.md") + RENAME + _diff("docs/b.md") + BIG
    joined = "".join(p.text for p in split.split_diff_by_module(text, 100_000))
    assert sorted(CHUNK.findall(joined)) == sorted(CHUNK.findall(text))
    assert len(joined) == len(text)


def test_quoted_and_non_ascii_paths_are_parsed():
    parts = split.split_diff_by_module(QUOTED + UNICODE + RENAME, 100_000)
    files = split.file_part_map(parts)
    assert "docs/dé x.txt" in files and "docs/über.md" in files
    assert "rust/worker/new.py" in files
    assert "(unparsed)" not in files
    assert {p.module for p in parts} == {"docs", "rust/worker"}


def test_oversized_single_file_stays_alone_and_whole():
    parts = split.split_diff_by_module(BIG, 100)
    assert len(parts) == 1 and parts[0].text == BIG


def test_headerless_input_is_kept_whole():
    parts = split.split_diff_by_module("not a diff\n", 10)
    assert "".join(p.text for p in parts) == "not a diff\n"


# R-4 --------------------------------------------------------------------


def test_one_failed_part_retries_on_the_next_family_for_that_part_only(tmp_path):
    diff = _diff("docs/x.md") + _diff("rust/worker/a.rs")
    qcalls, gcalls = [], []

    def qoder_run(prompt_text, model=None):
        qcalls.append(prompt_text)
        if "rust/worker/a.rs" in prompt_text.split("DIFF PART")[-1]:
            return qo("", "error", "rate limit", True)
        return qo(read("ok_pass.txt"))

    def gateway_call(model, prompt_text, title=None):
        gcalls.append(model)
        return gw(read("ok_pass.txt"))

    out = seat.run_seat(spec_for(tmp_path, "openai", diff=diff, files={}),
                        gateway_call=gateway_call, qoder_run=qoder_run,
                        opencode_run=lambda p, model=None: oc(read("ok_pass.txt")))
    assert len(qcalls) == 2 and len(gcalls) == 1          # part 1 not re-run
    by_part = {}
    for o in out:
        by_part.setdefault(o.part, []).append(o)
    assert [o.leg for o in by_part[0]] == ["qwen"] and by_part[0][0].tried == ["qwen"]
    assert [o.leg for o in by_part[1]] == ["qwen", "anthropic"]
    assert all(o.tried == ["qwen", "anthropic"] for o in by_part[1])
    assert by_part[0][0].pass_allowed and by_part[1][-1].pass_allowed


def test_leg_down_on_part_one_is_skipped_for_later_parts(tmp_path):
    diff = _diff("docs/x.md") + _diff("rust/worker/a.rs")
    qcalls = []

    def qoder_run(prompt_text, model=None):
        qcalls.append(1)
        return qo("", "error", "rate limit", True)

    out = seat.run_seat(spec_for(tmp_path, "openai", diff=diff, files={}),
                        gateway_call=lambda m, p, title=None: gw(read("ok_pass.txt")),
                        qoder_run=qoder_run, opencode_run=lambda p, model=None: oc(""))
    assert len(qcalls) == 1                                # qwen is down after part 1
    assert [o.part for o in out if o.pass_allowed] == [0, 1]


# R-5 --------------------------------------------------------------------


def test_opencode_prompt_after_double_dash_even_when_it_looks_like_a_flag():
    cmd = opencode.argv("--model evil", "p/m")
    assert cmd[-2:] == ["--", "--model evil"]
