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
    (tmp_path / "a.py").write_text("\n".join(f"a{i}" for i in range(1, 11)) + "\n")
    (tmp_path / "b.py").write_text("\n".join(f"b{i}" for i in range(1, 6)) + "\n")
    (tmp_path / "big.py").write_text("\n".join(f"x{i}" for i in range(500)) + "\n")
    return tmp_path


def test_slices_inlined_in_order_with_labels(tree):
    card = dict(CARD, context=["b.py:2-3", {"path": "a.py", "start": 1, "end": 2}])
    out = _Seat(tree)._leg_prompt("do it", card, "implement", INLINE)
    assert out.startswith("do it") and "CONTEXT PACK" in out
    assert out.index("b.py:2-3") < out.index("a.py:1-2")
    assert "```\nb2\nb3\n```" in out and "```\na1\na2\n```" in out and "omitted" not in out


def test_cap_drops_and_names_overflow(tree, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_CONTEXT_PACK_MAX_CHARS", "60")
    card = dict(CARD, context=["b.py:1-1", "a.py:1-10", "b.py:5-5"])
    out = _Seat(tree)._leg_prompt("p", card, "implement", INLINE)
    assert "b.py:1-1" in out and "```\nb5\n```" in out
    assert out.count("omitted:") == 1 and "omitted: a.py:1-10" in out
    assert "a9" not in out


@pytest.mark.parametrize("bad", ["../x.py:1-2", "/etc/passwd:1-2", "nope.py:1-2", "a.py:3-1", "a.py", 7])
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
