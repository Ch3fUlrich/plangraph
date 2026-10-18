"""Tests for the UI-VERSION-STAMP build-sha plumbing (task A, python side).

The rendered page must carry exactly one
``<meta name="plangraph-build" content="…">`` whose value comes from
``render.build_sha()``: env override, then ``git rev-parse``, then "unknown".
The sha must never leak into the embedded snapshot JSON.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENDOR = ROOT / "vendor"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "page_snapshot.json"

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402

SNAPSHOT = json.loads(FIXTURE.read_text(encoding="utf-8"))

META_RE = re.compile(r'<meta name="plangraph-build" content="([^"]*)"')


def html_for() -> str:
    return render_mod.render(SNAPSHOT, VENDOR)


def snapshot_script(html: str) -> str:
    m = re.search(
        r'<script[^>]*id="snapshot-data"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    assert m, "snapshot-data script block not found"
    return m.group(1)


class FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# --------------------------------------------------------------------------- #
# build_sha sources
# --------------------------------------------------------------------------- #

def test_build_sha_from_env(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", "  cafef00d1  ")
    assert render_mod.build_sha() == "cafef00d1"


def test_build_sha_env_wins_over_git(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", "envsha1234")
    monkeypatch.setattr(
        render_mod.subprocess, "run",
        lambda *a, **k: FakeCompleted("gitwouldwin\n"),
    )
    assert render_mod.build_sha() == "envsha1234"


def test_build_sha_from_git(monkeypatch):
    monkeypatch.delenv("PLANGRAPH_BUILD_SHA", raising=False)
    seen = {}

    def fake_run(cmd, *args, **kwargs):
        seen["cmd"] = cmd
        return FakeCompleted("abc1234567\n")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    assert render_mod.build_sha() == "abc1234567"
    assert seen["cmd"][:2] == ["git", "-C"]
    assert "rev-parse" in seen["cmd"]


def test_build_sha_git_failure_is_unknown(monkeypatch, tmp_path):
    monkeypatch.delenv("PLANGRAPH_BUILD_SHA", raising=False)

    def fake_run(cmd, *args, **kwargs):
        return FakeCompleted("", returncode=128)

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    assert render_mod.build_sha(pkg_dir=tmp_path) == "unknown"


def test_build_sha_non_git_dir_is_unknown(monkeypatch, tmp_path):
    monkeypatch.delenv("PLANGRAPH_BUILD_SHA", raising=False)
    assert render_mod.build_sha(pkg_dir=tmp_path) == "unknown"


def test_build_sha_sanitises_env_injection(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", '"><script>')
    assert render_mod.build_sha() == "unknown"


# --------------------------------------------------------------------------- #
# render() wiring
# --------------------------------------------------------------------------- #

def test_render_meta_tag_from_env(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", "beefcafe01")
    matches = META_RE.findall(html_for())
    assert matches == ["beefcafe01"]


def test_render_meta_tag_from_git(monkeypatch):
    monkeypatch.delenv("PLANGRAPH_BUILD_SHA", raising=False)
    monkeypatch.setattr(
        render_mod.subprocess, "run",
        lambda *a, **k: FakeCompleted("abc1234567\n"),
    )
    matches = META_RE.findall(html_for())
    assert matches == ["abc1234567"]


def test_render_meta_tag_present_exactly_once(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", "one1234567")
    html = html_for()
    assert html.count('<meta name="plangraph-build"') == 1
    assert META_RE.search(html)


def test_sha_not_in_snapshot_json(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", "notin12345")
    html = html_for()
    assert "notin12345" not in snapshot_script(html)


def test_injected_env_never_reaches_the_page(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_BUILD_SHA", '"><script>alert(1)</script>')
    html = html_for()
    assert '<meta name="plangraph-build" content="unknown">' in html
    assert "alert(1)" not in html
