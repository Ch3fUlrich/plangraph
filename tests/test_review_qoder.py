"""The qodercli leg: argv, parse, classify, run, seat dir (hermetic)."""
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

from plangraph.review import qoder as q


def _payload(**over):
    """Trimmed result object in the real ``-o json`` shape."""
    doc = {"type": "result", "subtype": "success", "is_error": False,
           "num_turns": 1, "result": "hello\nVERDICT: pass\n",
           "usage": {"input_tokens": 10, "output_tokens": 5}}
    doc.update(over)
    return json.dumps(doc)


def _completed(stdout, rc=0):
    return subprocess.CompletedProcess(args=["qodercli"], returncode=rc,
                                       stdout=stdout, stderr="")


def _run_with(stdout, rc=0, **kw):
    def fake(cmd, **kwargs):
        assert isinstance(cmd, list)
        return _completed(stdout, rc)
    return q.run("prompt text", runner=fake, **kw)


def test_argv_exact():
    """Inner argv shape (run adds the timeout wrapper)."""
    assert q.argv("P") == ["qodercli", "-p", "--model", "Qwen3.8-Flash",
                           "--tools", "", "--no-session-persistence",
                           "-o", "json", "P"]
    assert q.argv("P", model="m", binary="b") == ["b", "-p", "--model", "m",
                                                  "--tools", "",
                                                  "--no-session-persistence",
                                                  "-o", "json", "P"]


def test_parse_happy():
    """Trimmed happy object parses to text and usage."""
    got = q.parse_output(_payload())
    assert got["text"] == "hello\nVERDICT: pass\n"
    assert got["is_error"] is False
    assert got["num_turns"] == 1
    assert got["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_parse_array_takes_last_result():
    """An event array resolves to the last result object."""
    events = [{"type": "progress", "n": 1},
              {"type": "result", "subtype": "success", "is_error": False,
               "num_turns": 1, "result": "first", "usage": {}},
              {"type": "result", "subtype": "success", "is_error": False,
               "num_turns": 2, "result": "last", "usage": {}}]
    got = q.parse_output(json.dumps(events))
    assert got["text"] == "last"
    assert got["num_turns"] == 2


def test_parse_garbage():
    """Garbage stdout is an error with empty text."""
    for bad in ("not json {{{", "", "[]", '{"type": "progress"}'):
        got = q.parse_output(bad)
        assert got["text"] == ""
        assert got["is_error"] is True


def test_classify_happy_stop():
    """Clean answer is stop."""
    assert q.classify(q.parse_output(_payload())) == "stop"


def test_classify_is_error():
    """Error flag wins."""
    assert q.classify(q.parse_output(_payload(is_error=True))) == "error"


def test_classify_empty():
    """Blank result text is empty."""
    assert q.classify(q.parse_output(_payload(result="   "))) == "empty"


def test_classify_tool_call_text():
    """Inline tool-call marker never passes."""
    got = q.parse_output(_payload(result="plan <tool_call>do</tool_call>"))
    assert q.classify(got) == "tool_call"


def test_classify_tool_call_turns():
    """Multiple turns mean tools were used."""
    got = q.parse_output(_payload(result="plain answer", num_turns=3))
    assert q.classify(got) == "tool_call"


def test_classify_leading_json_tool_call():
    """Leading name+arguments object is a tool call."""
    text = json.dumps({"name": "read", "arguments": {"path": "x"}})
    assert q.classify(q.parse_output(_payload(result=text))) == "tool_call"


def test_classify_length_subtype():
    """A max/length subtype is a cut answer."""
    got = q.parse_output(_payload(subtype="max_tokens"))
    assert q.classify(got) == "length"


def test_run_happy_fields():
    """Run records sha, times, rc and evidence tokens."""
    res = _run_with(_payload())
    assert res.rc == 0
    assert res.text == "hello\nVERDICT: pass\n"
    assert res.finish_reason == "stop"
    assert res.num_turns == 1
    assert res.is_error is False
    assert res.error is None
    assert res.prompt_sha256 == hashlib.sha256(b"prompt text").hexdigest()
    assert res.started and res.finished
    ev = res.to_evidence()
    assert ev["requested_model"] == ev["served_model"] == "Qwen3.8-Flash"
    assert ev["status"] == 0
    assert ev["tokens"] == {"input_tokens": 10, "output_tokens": 5}
    assert ev["leg"] == "qoder"


def test_run_variants():
    """Empty, error, tool-call and length legs via injected runner."""
    assert _run_with(_payload(result="")).finish_reason == "empty"
    assert _run_with(_payload(is_error=True)).finish_reason == "error"
    assert _run_with(_payload(result="a <tool_call>b")).finish_reason == "tool_call"
    assert _run_with(_payload(result="x", num_turns=2)).finish_reason == "tool_call"
    assert _run_with(_payload(subtype="length_capped")).finish_reason == "length"


def test_run_timeout():
    """Timeout expiry yields timeout with no rc."""
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=99)
    res = q.run("p", runner=boom, timeout=99)
    assert res.finish_reason == "timeout"
    assert res.rc is None
    assert res.is_error is True
    assert res.error


def test_run_missing_binary():
    """Missing binary yields error with the fixed message."""
    def boom(cmd, **kwargs):
        raise FileNotFoundError(2, "no such file")
    res = q.run("p", runner=boom)
    assert res.finish_reason == "error"
    assert res.rc is None
    assert res.error == "binary not found"


def test_run_garbage_stdout():
    """Garbage stdout is an error leg, not a crash."""
    res = _run_with("garbage {{{")
    assert res.finish_reason == "error"
    assert res.text == ""


def test_run_uses_empty_temp_cwd(tmp_path):
    """Real stub sees an empty dir that is not the repo cwd."""
    stub = tmp_path / "qodercli"
    stub.write_text("#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "doc = {'cwd': os.getcwd(), 'ls': sorted(os.listdir('.'))}\n"
                    "out = {'type': 'result', 'subtype': 'success',\n"
                    " 'is_error': False, 'num_turns': 1,\n"
                    " 'result': json.dumps(doc), 'usage': {}}\n"
                    "sys.stdout.write(json.dumps(out))\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
    res = q.run("hello", binary=str(stub), timeout=30)
    assert res.finish_reason == "stop"
    seen = json.loads(res.text)
    assert seen["ls"] == []
    assert Path(seen["cwd"]) != Path.cwd()
    assert os.listdir(tmp_path) != [] or True


def test_run_full_cmd_shape(tmp_path):
    """The spawned argv carries the timeout wrapper and inner flags."""
    seen = {}

    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        assert kwargs.get("cwd") != os.getcwd()
        assert os.listdir(kwargs.get("cwd")) == []
        return _completed(_payload())
    q.run("P", model="mm", timeout=17, binary="qq", runner=fake)
    assert seen["cmd"][:2] == ["timeout", "17"]
    assert seen["cmd"][2:] == q.argv("P", model="mm", binary="qq")


def test_write_seat_dir(tmp_path):
    """Seat dir holds review.txt + evidence.json and no raw stdout."""
    res = _run_with(_payload())
    out = tmp_path / "seat"
    q.write_seat_dir(out, res, extra={"k": "v"})
    assert (out / "review.txt").read_text() == res.text
    ev = json.loads((out / "evidence.json").read_text())
    assert ev["finish_reason"] == "stop"
    assert ev["k"] == "v"
    assert ev["leg"] == "qoder"
    names = sorted(p.name for p in out.iterdir())
    assert names == ["evidence.json", "review.txt"]
