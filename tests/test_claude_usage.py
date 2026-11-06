import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("claude_usage", Path(__file__).parents[1] / "tools/claude_usage.py")
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)


def assistant(mid="m1", text="", read=100, output=10, timestamp="2026-11-06T00:00:00Z"):
    return dict(type="assistant", timestamp=timestamp, message=dict(
        id=mid, model="example-model", content=[dict(type="text", text=text)],
        usage=dict(input_tokens=20, output_tokens=output,
                   cache_creation_input_tokens=30, cache_read_input_tokens=read)))


@pytest.fixture
def tree(tmp_path):
    project = tmp_path / "projects" / "example-project"
    project.mkdir(parents=True)
    return project


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def run(tree, capsys, *args, code=0):
    assert usage.main(["--projects", str(tree.parent), *args, "--json"]) == code
    return json.loads(capsys.readouterr().out)


def test_v1_duplicates_subagents_role(tree, capsys):
    write(tree / "s1.jsonl", [dict(type="custom-title", customTitle="routing-0191",
                                   timestamp="2026-11-06T00:00:00Z"),
                                assistant(output=1), assistant(output=20)])
    write(tree / "s1/subagents/agent-a.jsonl", [assistant("sub")])
    report = run(tree, capsys, "roles")["result"]["routing"]
    assert report["requests"] == 2
    assert report["weighted"] == 150
    assert report["sessions"] == report["starts"] == 1
    assert report["subagent_share"] == 70 / 150


def test_pace_quiet_and_over(tree, capsys):
    write(tree / "s.jsonl", [assistant()])
    assert usage.main(["--projects", str(tree.parent), "pace", "--day", "2026-11-06"]) == 0
    assert capsys.readouterr().out == ""
    write(tree / "s.jsonl", [assistant(output=100000000)])
    assert usage.main(["--projects", str(tree.parent), "pace", "--day", "2026-11-06"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("over pace: 2026-11-06 weighted=")
    assert len(output.splitlines()) == 1
    assert usage.UNSEEN in output
    assert run(tree, capsys, "pace", "--day", "2026-11-06")["result"]["over_pace"]


def test_weights_median(tree, tmp_path, capsys):
    write(tree / "s.jsonl", [assistant(str(i), read=n) for i, n in enumerate([10, 1000, 30, 50])])
    target = tmp_path / "weights.json"
    result = run(tree, capsys, "weights", "--baseline-since", "2026-11-06",
                 "--baseline-until", "2026-11-07", "--out", str(target))
    assert result["result"] == {"unnamed": 4}
    assert json.loads(target.read_text()) == result["result"]


def inputs(tmp_path):
    files = {}
    for name, text in dict(a="FLEET-LAUNCH\nSECOND\n", b="THIRD\n",
                           landed="FLEET-LAUNCH\nTHIRD\n", sessions="owned\n").items():
        path = tmp_path / name
        path.write_text(text)
        files[name] = str(path)
    fixed = tmp_path / "fixed.json"
    fixed.write_text('{"unnamed": 5}')
    files["fixed"] = str(fixed)
    return files


def test_v3_v5_prorata_whole_token_fixed(tree, tmp_path, capsys):
    f = inputs(tmp_path)
    write(tree / "shared.jsonl", [
        dict(type="user", message=dict(content="plangraph:FLEET-LAUNCH SECOND")),
        assistant(text="THIRD", read=300),
        assistant("m2", text="FLEET-LAUNCH-ADMISSION fleet-launch"),
        assistant("m3", text="OTHER:FLEET-LAUNCHER")])
    report = run(tree, capsys, "keys", "--arm", "A=" + f["a"], "--arm", "B=" + f["b"],
                 "--weights", f["fixed"], "--landed", f["landed"])["result"]
    assert report["A"]["turns"] == pytest.approx(2 / 3)
    assert report["B"]["turns"] == pytest.approx(1 / 3)
    assert report["A"]["actual"] == 60
    assert report["A"]["fixed"] == pytest.approx(65 * 2 / 3)
    assert report["A"]["tokens_per_accepted_actual"] == 60
    assert report["A"]["keys_named"] == ["FLEET-LAUNCH", "SECOND"]


def test_session_not_double_counted_includes_subagent(tree, tmp_path, capsys):
    f = inputs(tmp_path)
    write(tree / "owned.jsonl", [assistant(text="THIRD FLEET-LAUNCH")])
    write(tree / "owned/subagents/agent-x.jsonl", [assistant("sub", text="THIRD")])
    write(tree / "shared.jsonl", [assistant(text="THIRD")])
    report = run(tree, capsys, "keys", "--arm", "A=" + f["a"] + "," + f["sessions"],
                 "--arm", "B=" + f["b"], "--weights", f["fixed"], "--landed", f["landed"])["result"]
    assert report["A"]["turns"] == 2
    assert report["A"]["actual"] == report["A"]["fixed"] == 140
    assert report["B"]["turns"] == 1


@pytest.mark.parametrize("failure", ["zero", "empty", "missing", "no-transcripts"])
def test_v4_no_result(tree, tmp_path, capsys, failure):
    f = inputs(tmp_path)
    if failure != "no-transcripts":
        write(tree / "s.jsonl", [assistant(text="SECOND")])
    if failure == "empty":
        Path(f["a"]).write_text("")
    if failure == "missing":
        f["a"] = str(tmp_path / "absent")
    result = run(tree, capsys, "keys", "--arm", "A=" + f["a"],
                 "--weights", f["fixed"], code=3)
    assert result.get("status", result.get("result", {}).get("A", {}).get("status")) == "no result"
    assert "result" not in result or failure == "zero"


def test_malformed_and_utc_boundary(tree, capsys):
    path = tree / "s.jsonl"
    write(path, [assistant(timestamp="2026-11-06T00:30:00+01:00"),
                 assistant("m2", timestamp="2026-11-06T00:00:00Z")])
    with path.open("a") as handle:
        handle.write("not json\n")
    result = run(tree, capsys, "daily", "--since", "2026-11-05", "--until", "2026-11-07")
    assert result["malformed_lines"] == 1
    assert result["result"]["2026-11-05"]["requests"] == 1
    assert result["result"]["2026-11-06"]["requests"] == 1


def test_v2_read_only_and_no_text(tree, capsys, monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("external execution forbidden")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    write(tree / "s.jsonl", [assistant(text="PRIVATE_TRANSCRIPT_SENTINEL")])
    before = (tree / "s.jsonl").read_bytes()
    result = run(tree, capsys, "daily")
    assert "PRIVATE_TRANSCRIPT_SENTINEL" not in json.dumps(result)
    assert (tree / "s.jsonl").read_bytes() == before


def test_v6_no_machine_identifiers():
    source = Path(usage.__file__).read_text()
    assert "subprocess" not in source
    assert "socket" not in source
    assert "/home/" not in source


def test_streaming_reader(tree, capsys, monkeypatch):
    write(tree / "s.jsonl", [assistant()])
    original = Path.open

    class StreamOnly:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return iter(self.handle)

        def __exit__(self, *args):
            self.handle.close()

    def opened(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return StreamOnly(handle) if path.suffix == ".jsonl" else handle

    monkeypatch.setattr(Path, "open", opened)
    assert run(tree, capsys, "daily")["result"]["2026-11-06"]["requests"] == 1


@pytest.mark.parametrize("as_dict", [False, True])
def test_snapshot_tool_input_and_unlanded_numerator(tree, tmp_path, capsys, as_dict):
    f = inputs(tmp_path)
    first = assistant(text="", read=200)
    first["message"]["content"] = [dict(type="tool_use", input=dict(query="FLEET-LAUNCH"))]
    write(tree / "s.jsonl", [first, dict(type="user", message=dict(content=[
        dict(type="tool_result", content="SECOND")])), assistant("m2")])
    snapshot = tmp_path / "snapshot.json"
    item = dict(key="plangraph:FLEET-LAUNCH", state="done")
    snapshot.write_text(json.dumps(dict(items={"one": item} if as_dict else [item])))
    report = run(tree, capsys, "keys", "--arm", "A=" + f["a"],
                 "--snapshot", str(snapshot))["result"]["A"]
    assert report["accepted"] == 1
    assert report["actual"] == report["tokens_per_accepted_actual"] == 150
    assert report["turns"] == 2


def test_nested_typed_tool_input_attribution(tree, tmp_path, capsys):
    f = inputs(tmp_path)
    record = assistant()
    record["message"]["content"] = [dict(type="tool_use", input={
        "edits": [{"type": "replace", "old_string": "FLEET-LAUNCH"}]})]
    write(tree / "s.jsonl", [record])
    report = run(tree, capsys, "keys", "--arm", "A=" + f["a"],
                 "--landed", f["landed"])
    assert report["malformed_lines"] == 0
    assert report["result"]["A"]["turns"] == 1
    assert report["result"]["A"]["keys_named"] == ["FLEET-LAUNCH"]


@pytest.mark.parametrize("kind", ["user", "tool_result"])
def test_string_message_attribution(tree, tmp_path, capsys, kind):
    f = inputs(tmp_path)
    write(tree / "s.jsonl", [dict(type=kind, message="plangraph:FLEET-LAUNCH",
                                  content="THIRD"), assistant()])
    report = run(tree, capsys, "keys", "--arm", "A=" + f["a"],
                 "--landed", f["landed"])
    assert report["malformed_lines"] == 0
    assert report["result"]["A"]["turns"] == 1
    assert report["result"]["A"]["keys_named"] == ["FLEET-LAUNCH"]


def test_last_record_controls_day_and_usage(tree, capsys):
    write(tree / "s.jsonl", [assistant(timestamp="2026-11-05T23:59:59Z"),
                             assistant(output=40, timestamp="2026-11-06T00:00:00Z")])
    result = run(tree, capsys, "daily")["result"]
    assert list(result) == ["2026-11-06"]
    assert result["2026-11-06"]["weighted"] == 100


@pytest.mark.parametrize("location", ["parent", "subagent"])
def test_session_mirrors_last_record_and_sidechain(tree, capsys, location):
    first = assistant(output=1, timestamp="2026-11-05T23:59:59Z")
    last = assistant(output=40)
    last["isSidechain"] = location == "parent"
    write(tree / "s.jsonl", [first, last] if location == "parent" else [first])
    if location == "subagent":
        write(tree / "s/subagents/agent-a.jsonl", [last])
    # The same message id in a different session remains a separate request.
    write(tree / "other.jsonl", [assistant(output=10)])
    report = run(tree, capsys, "roles")["result"]["unnamed"]
    assert report["requests"] == 2
    assert report["weighted"] == 170
    assert report["subagent_share"] == 100 / 170
    days = run(tree, capsys, "daily")["result"]
    assert list(days) == ["2026-11-06"]


@pytest.mark.parametrize("failure", ["no-turns", "zero-accepted"])
def test_arm_no_result_preserves_other_arm(tree, tmp_path, capsys, failure):
    f = inputs(tmp_path)
    write(tree / "s.jsonl", [assistant(text="FLEET-LAUNCH")])
    if failure == "zero-accepted":
        Path(f["landed"]).write_text("FLEET-LAUNCH\n")
        write(tree / "other.jsonl", [assistant(text="THIRD")])
    report = run(tree, capsys, "keys", "--arm", "A=" + f["a"],
                 "--arm", "B=" + f["b"], "--landed", f["landed"], code=3)["result"]
    assert report["A"]["status"] == "ok"
    assert report["A"]["actual"] == report["A"]["tokens_per_accepted_actual"] == 70
    assert report["B"]["status"] == "no result"
    assert "tokens_per_accepted_actual" not in report["B"]
    assert report["B"]["turns"] == (1 if failure == "zero-accepted" else 0)
    assert report["B"]["actual"] == (70 if failure == "zero-accepted" else 0)


def test_start_after_metadata(tree, capsys):
    write(tree / "s.jsonl", [dict(type="agent-name", agentName="worker-12"), assistant()])
    assert run(tree, capsys, "roles")["result"]["worker"]["starts"] == 1


@pytest.mark.parametrize("bad", ["empty", "unreadable"])
def test_bad_transcript_isolated(tree, capsys, monkeypatch, bad):
    write(tree / "good.jsonl", [assistant()])
    write(tree / "bad.jsonl", [])
    original = Path.open
    if bad == "unreadable":
        def opened(path, *args, **kwargs):
            if path.name == "bad.jsonl":
                raise PermissionError()
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", opened)
    report = run(tree, capsys, "roles")
    assert report["result"]["unnamed"]["requests"] == 1
    assert report[bad + "_files"] == 1
    (tree / "good.jsonl").unlink()
    assert run(tree, capsys, "roles", code=3)["status"] == "no result"


def test_window_roles_and_aligned_text(tree, capsys):
    for name, day, output in [("old", "05", 10), ("small", "06", 10), ("large", "06", 40)]:
        write(tree / (name + ".jsonl"), [dict(type="agent-name", agentName=name),
              assistant(output=output, timestamp=f"2026-11-{day}T00:00:00Z")])
    report = run(tree, capsys, "roles", "--since", "2026-11-06")["result"]
    assert set(report) == {"small", "large"}
    daily = run(tree, capsys, "daily")["result"]
    assert set(daily["2026-11-06"]["per_role"]) == {"small", "large"}
    for command in ("roles", "daily"):
        assert usage.main(["--projects", str(tree.parent), command, "--since", "2026-11-06"]) == 0
        text = capsys.readouterr().out.splitlines()
        assert "weighted (M)" in text[0]
        assert text[1].startswith("large" if command == "roles" else "2026-11-06")
        assert "0.000" in text[1]


def test_mtime_skips_old_files_including_subagents(tree, capsys, monkeypatch):
    import os
    for path in [tree / "old.jsonl", tree / "s.jsonl", tree / "s/subagents/agent-a.jsonl"]:
        write(path, [assistant()])
        if path.name != "s.jsonl":
            os.utime(path, (1, 1))
    original = Path.open
    def opened(path, *args, **kwargs):
        assert path.suffix != ".jsonl" or path.name == "s.jsonl"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    assert run(tree, capsys, "roles", "--since", "2026-11-06")["result"]["unnamed"]["requests"] == 1
    assert run(tree, capsys, "pace", "--day", "2026-11-06")["result"]["weighted"] == 70
