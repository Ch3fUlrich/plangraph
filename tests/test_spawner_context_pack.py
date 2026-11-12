"""D-3622: context pack for inline-only legs and the big-file brief warning (hermetic)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plangraph.routing_errors import ParameterRefused
from plangraph.spawner import context_pack
from plangraph.spawner.seat_spawn import SeatSpawnMixin

from registry_route_util import CARD, _Runner, _seeded, _service  # noqa: F401

INLINE = SimpleNamespace(tool_mode="inline-only", max_request_tokens=100000)
TOOLS = SimpleNamespace(tool_mode="tools", max_request_tokens=100000)


class _Seat(SeatSpawnMixin):
    def __init__(self, cwd):
        self.runner = SimpleNamespace(default_cwd=str(cwd))


@pytest.fixture
def tree(tmp_path):
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "a.py").write_text("\n".join(f"a{i}" for i in range(1, 11)) + "\n")
    (tmp_path / "b.py").write_text("\n".join(f"b{i}" for i in range(1, 6)) + "\n")
    (tmp_path / "big.py").write_text("\n".join(f"x{i}" for i in range(500)) + "\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_text("\n".join(f"t{i}" for i in range(450)) + "\n")
    (tmp_path / "v1.2.cfg").write_text("\n".join(f"c{i}" for i in range(450)) + "\n")
    (tmp_path / "a.token").write_text("tok\n")
    (tmp_path / "untracked.py").write_text("u\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py", "b.py", "big.py", "bin", "v1.2.cfg", "a.token"], check=True)
    return tmp_path


def test_slices_inlined_in_order_with_labels(tree):
    card = dict(CARD, context=["b.py:2-3", {"path": "a.py", "start": 1, "end": 2}])
    out = _Seat(tree)._leg_prompt("do it", card, "implement", INLINE)
    assert out.startswith("do it") and "CONTEXT PACK" in out
    assert out.index("b.py:2-3") < out.index("a.py:1-2")
    assert "```\nb2\nb3\n```" in out and "```\na1\na2\n```" in out and "omitted" not in out


def test_cap_drops_and_names_overflow(tree, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_CONTEXT_PACK_MAX_CHARS", "130")
    card = dict(CARD, context=["b.py:1-1", "a.py:1-10", "b.py:5-5"])
    out = _Seat(tree)._leg_prompt("p", card, "implement", INLINE)
    assert "b.py:1-1" in out and "```\nb5\n```" in out
    assert out.count("omitted:") == 1 and "omitted: a.py:1-10" in out
    assert "a9" not in out


@pytest.mark.parametrize("bad", ["../x.py:1-2", "/" + "et" + "c/pass" + "wd:1-2", "nope.py:1-2", "a.py:3-1", "a.py", 7])
def test_bad_context_refused(tree, bad):
    with pytest.raises(ParameterRefused):
        context_pack.annotate({}, "p", dict(CARD, context=[bad]), tree)


def test_symlink_out_of_cwd_refused(tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("s\n")
    (tree / "link.txt").symlink_to(outside)
    with pytest.raises(ParameterRefused, match="symlink"):
        context_pack.annotate({}, "p", dict(CARD, context=["link.txt:1-1"]), tree)


def test_tool_leg_gets_no_pack(tree):
    out = _Seat(tree)._leg_prompt("do it", dict(CARD, context=["a.py:1-2"]), "implement", TOOLS)
    assert out == "do it"


def test_big_named_file_warns_in_spawn_result(tree, tmp_path, monkeypatch):
    svc = _service(tmp_path / "state", monkeypatch)
    svc.runner.default_cwd = str(tree)
    out = asyncio.run(svc.spawn(card=dict(CARD), prompt="edit big.py and a.py", dry_run=True))
    assert len(out["warnings"]) == 1 and "big.py 500 lines" in out["warnings"][0]
    assert any(e["event"] == "big_file_unsliced" for e in out["events"])
    assert out["status"] != "refused"


def test_sliced_big_file_no_warning(tree, tmp_path, monkeypatch):
    svc = _service(tmp_path / "state", monkeypatch)
    svc.runner.default_cwd = str(tree)
    out = asyncio.run(svc.spawn(card=dict(CARD, context=["big.py:1-5"]), prompt="edit big.py", dry_run=True))
    assert "warnings" not in out


@pytest.mark.parametrize("path,why", [("a.token", "secret"), ("untracked.py", "not tracked")])
def test_secret_or_untracked_skipped_unread(tree, path, why):
    card = dict(CARD, context=[path + ":1-1", "b.py:1-1"])
    out = _Seat(tree)._leg_prompt("p", card, "implement", INLINE)
    assert "tok" not in out and "```\nu\n" not in out and "b1" in out
    assert any("context_path_refused: " + path in w and why in w for w in context_pack.annotate({}, "p", card, tree).warnings)


@pytest.mark.parametrize("cap", [0, 80, 150, 200, 260, 400, 1000])
def test_pack_never_exceeds_cap(tree, monkeypatch, cap):
    monkeypatch.setenv("PLANGRAPH_CONTEXT_PACK_MAX_CHARS", str(cap))
    card = dict(CARD, context=["a.py:1-10", "b.py:1-5", "a.py:1-3"])
    out = _Seat(tree)._leg_prompt("", card, "implement", INLINE)
    assert len(out) <= cap


def test_omitted_line_forces_extra_drop(tree, monkeypatch):
    card = dict(CARD, context=["b.py:1-1", "b.py:2-2"])
    full = len(_Seat(tree)._leg_prompt("", card, "implement", INLINE))
    partial = 0
    for cap in range(full - 1, full - 80, -1):
        monkeypatch.setenv("PLANGRAPH_CONTEXT_PACK_MAX_CHARS", str(cap))
        out = _Seat(tree)._leg_prompt("", card, "implement", INLINE)
        assert len(out) <= cap
        if out:
            assert "omitted: b.py:2-2" in out and "--- b.py:1-1 ---" in out
            partial += 1
    assert partial > 0


def test_big_file_detection_without_extension_regex(tree):
    found = context_pack.warnings("run `bin/tool` and (v1.2.cfg), see untracked.py", dict(CARD), tree)
    assert any("bin/tool 450" in w for w in found) and any("v1.2.cfg 450" in w for w in found)


def test_unfit_framing_returns_prompt_with_warning(tree, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_CONTEXT_PACK_MAX_CHARS", "20")
    card = dict(CARD, context=["b.py:1-1"])
    assert _Seat(tree)._leg_prompt("p", card, "implement", INLINE) == "p"
    found = context_pack.annotate({}, "p", card, tree).warnings
    assert len(found) == 1 and found[0].startswith("context_pack_unfit: cap 20 chars, needs ")


def test_run_cwd_is_repo_subdirectory(tree):
    import subprocess
    sub = tree / "pkg"
    sub.mkdir()
    (sub / "m.py").write_text("\n".join(f"m{i}" for i in range(450)) + "\n")
    subprocess.run(["git", "-C", str(tree), "add", "pkg"], check=True)
    out = _Seat(sub)._leg_prompt("p", dict(CARD, context=["m.py:1-2"]), "implement", INLINE)
    assert "m.py:1-2" in out and "m1" in out
    with pytest.raises(ParameterRefused):
        context_pack.annotate({}, "p", dict(CARD, context=["../a.py:1-1"]), sub)
    found = context_pack.warnings("see pkg/m.py and m.py", dict(CARD), sub)
    assert any("m.py 450 lines" in w for w in found)
    assert context_pack.warnings("m.py", dict(CARD, context=["m.py:1-2"]), sub) == []


@pytest.mark.parametrize('data', [b'', b'a', b'a\n', b'a\nb', b'a\r\nb\r\n', b'x\n' * 70000 + b'tail'],
                         ids=['empty', 'single', 'trailing-newline', 'two-lines', 'crlf', 'multi-chunk'])
def test_line_count_streams_and_matches_splitlines(tmp_path, monkeypatch, data):
    path = tmp_path / 'f.txt'
    path.write_bytes(data)
    monkeypatch.setattr(context_pack.Path, 'read_text', lambda *a, **k: pytest.fail('read whole file'))
    assert context_pack._line_count(path) == len(data.decode().splitlines())


def test_range_mention_without_slice_still_warns(tree):
    for brief in ("edit big.py:1-50 now", "see big.py:7.", "see big.py:1-50:"):
        found = context_pack.warnings(brief, dict(CARD), tree)
        assert [w for w in found if w.startswith("big_file_unsliced: big.py ")], brief
