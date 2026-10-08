"""Probes — the NEW UI's CDN mode and its §14b offline refusal (Task T1).

UI-PLAN §14c / line 20: the new page replaces dagre + cytoscape-dagre with two
runtime libraries, both fetched from public CDNs with ``defer``:

* cytoscape 3.34.0 from cdnjs (`render.CDN_LIBS`, plan line 18), and
* elkjs 0.9.3 from jsDelivr (`render.CDN_ELK_URL`, plan line 19) — the layout
  authority of §2c, EPL-2.0, licensed for CDN loading only in v1 (§14b).

The probes never restate a URL twice: every assertion is either a comparison
against `render.CDN_LIBS` / `render.CDN_ELK_URL` / `render.OFFLINE_ERROR` or a
literal of the exact plan URL, so swapping a CDN upstream is a one-line change.

Offline (``cdn=False``) keeps working for *legacy* snapshots (no
``phase``/``loop``/``questions``/``decisions`` markers) so the WP3 page stays
published as one self-contained artifact for ``tests/test_render.py``; a
new-UI snapshot raises ``RuntimeError(render.OFFLINE_ERROR)`` (§14b) before
anything is written, because the page's boot bundle needs page_model/
page_store/page_search plus elkjs and the old inline path is not kept.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENDOR = ROOT / "vendor"
TESTS = ROOT / "tests"

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402

NEW_SNAPSHOT = TESTS / "fixtures" / "ui" / "synthetic_snapshot.json"
LEGACY_SNAPSHOT = TESTS / "fixtures" / "page_snapshot.json"
NEW = json.loads(NEW_SNAPSHOT.read_text(encoding="utf-8"))
LEGACY = json.loads(LEGACY_SNAPSHOT.read_text(encoding="utf-8"))

#: The only external hosts any rendered page may reference (§ constraints V1).
ALLOWED_HOSTS = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net")

#: The legacy vendored trio — CDN mode must inline none of it.
VENDOR_FILES = ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js")

#: cdnjs coordinates as fixed by plan line 18 (authoritative literal).
PLAN_CYTOSCAPE_URL = (
    "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.34.0/cytoscape.min.js"
)


def tags_of(document: str) -> list[str]:
    return re.findall(r"<script\b[^>]*>", document, re.IGNORECASE)


def src_urls(document: str) -> list[str]:
    return re.findall(r'<script\b[^>]*\ssrc="([^"]+)"', document, re.IGNORECASE)


def embedded(document: str) -> dict:
    m = re.search(
        r'<script[^>]*id="snapshot-data"[^>]*>(.*?)</script>',
        document,
        re.DOTALL | re.IGNORECASE,
    )
    assert m, "snapshot-data block missing"
    return json.loads(m.group(1))


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    cmd = [sys.executable, "-m", "plangraph.render", *args]
    return subprocess.run(
        cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180
    )


# --------------------------------------------------------------------------- #
# the tag list (a constant, never a literal in the HTML path)
# --------------------------------------------------------------------------- #

def test_cdn_script_tags_reconstruct_from_constants():
    """`cdn_script_tags()` is exactly the mapped constants — cytoscape first,
    elkjs second, both `defer` (plan line 20)."""
    expected = [
        f'<script defer src="{render_mod.CDN_BASE}/{lib}/{version}/{filename}"></script>'
        for lib, version, filename in render_mod.CDN_LIBS
    ] + [f'<script defer src="{render_mod.CDN_ELK_URL}"></script>']
    tags = render_mod.cdn_script_tags()
    assert tags == expected
    assert len(tags) == 2
    assert all(' defer ' in t and t.startswith("<script ") for t in tags)


def test_urls_are_the_plan_urls_and_dagre_is_gone():
    """Plan line 18/19 literals; dagre + cytoscape-dagre tags removed (line 20)."""
    assert render_mod.CDN_ELK_URL == (
        "https://cdn.jsdelivr.net/npm/elkjs@0.9.3/lib/elk.bundled.js"
    )
    tags = render_mod.cdn_script_tags()
    assert PLAN_CYTOSCAPE_URL in tags[0]
    assert render_mod.CDN_ELK_URL in tags[1]
    all_urls = " ".join(tags)
    assert "dagre" not in all_urls
    assert "ajax/libs/dagre" not in all_urls       # the old cdnjs dagre path
    assert "npm/cytoscape-dagre@" not in all_urls  # the old jsDelivr path


# --------------------------------------------------------------------------- #
# render(cdn=True) structure
# --------------------------------------------------------------------------- #

def test_cdn_html_carries_exactly_the_two_defer_tags():
    """cdn mode: exactly two <script src> tags, hosts in the allow-list, both
    defer, none inline-referenced from vendor/."""
    document = render_mod.render(NEW, VENDOR, cdn=True)
    urls = src_urls(document)
    assert urls == [t.split('"')[1] for t in render_mod.cdn_script_tags()]
    assert len(urls) == 2
    for url in urls:
        assert url.startswith("https://")
        assert url.split("/")[2] in ALLOWED_HOSTS
        tag = next(t for t in tags_of(document) if url in t)
        assert "defer" in tag.lower()
    # nothing else on the page reaches out to the network
    for url in re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', document):
        assert url in urls
    # the dagre / cytoscape-dagre tags are gone from the document
    assert "ajax/libs/dagre" not in document
    assert "cytoscape-dagre@" not in document


def test_cdn_html_inlines_data_and_page_code_but_no_vendor():
    """The snapshot and all six page files (page.js, page_ui.js, page_details.js
    and the three new pure-core files) stay inline; no vendored lib source does."""
    document = render_mod.render(NEW, VENDOR, cdn=True)
    for name in VENDOR_FILES:
        body = (VENDOR / name).read_text(encoding="utf-8")
        assert body not in document, f"{name} must not be inlined in cdn mode"
    for name in render_mod.JS_FILES:
        body = render_mod.ASSETS_DIR.joinpath(name)
        assert body.is_file(), f"{name} is registered but missing"
        assert body.read_text(encoding="utf-8") in document, f"{name} not inlined"
    css = render_mod.ASSETS_DIR.joinpath("page.css")
    assert css.read_text(encoding="utf-8") in document
    assert embedded(document) == NEW  # round-trips exactly


def test_js_and_css_are_data_driven():
    """T1: render.py exposes JS_FILES / CSS_FILES tuples (line 135)."""
    assert render_mod.JS_FILES == (
        "page.js",
        "page_ui.js",
        "page_details.js",
        "page_model.js",
        "page_store.js",
        "page_search.js",
        "page_side.js",
        "page_graph.js",
        "page_graph_ext.js",
        "page_graph_cluster.js",
        "page_graph_budget.js",
        "page_graph_chips.js",
        "page_graph_focus.js",
        "page_layout.js",
        "page_graph_viewport.js",
        "page_graph_pan.js",
        "page_graph_view.js",
        "page_table_model.js",
        "page_table_url.js",
        "page_boot.js",
    )
    assert render_mod.CSS_FILES == ("page.css", "page_base.css", "page_graph.css")
    for name in (*render_mod.JS_FILES, *render_mod.CSS_FILES):
        assert (render_mod.ASSETS_DIR / name).is_file()


def test_cdn_mode_needs_no_vendor_directory(tmp_path):
    """CDN mode reads no vendor file, so an empty vendor dir works (§14)."""
    document = render_mod.render(NEW, tmp_path, cdn=True)
    assert src_urls(document) == [
        t.split('"')[1] for t in render_mod.cdn_script_tags()
    ]


# --------------------------------------------------------------------------- #
# legacy default path stays exactly as it is today (tests/test_render.py)
# --------------------------------------------------------------------------- #

def test_default_is_unchanged_and_has_no_cdn():
    explicit = render_mod.render(LEGACY, VENDOR, cdn=False)
    implicit = render_mod.render(LEGACY, VENDOR)
    assert explicit == implicit
    assert "cdnjs.cloudflare.com" not in explicit
    assert "cdn.jsdelivr.net" not in explicit
    assert src_urls(explicit) == []
    for name in VENDOR_FILES:
        assert (VENDOR / name).read_text(encoding="utf-8") in explicit
    assert embedded(explicit) == LEGACY


def test_legacy_snapshot_has_no_new_ui_markers():
    """Guards the marker rule: the legacy fixture must stay on the offline path."""
    assert render_mod._new_ui(LEGACY) is False
    assert render_mod._new_ui(NEW) is True
    assert render_mod._new_ui({"items": [{"title": "x", "phase": "dev"}]}) is True
    assert render_mod._new_ui({"items": [{"title": "x"}], "questions": []}) is True
    assert render_mod._new_ui({"items": [{"title": "x", "loop": 1}]}) is True
    assert render_mod._new_ui({"items": [{"title": "x"}]}) is False
    assert render_mod._new_ui({"items": []}) is False


# --------------------------------------------------------------------------- #
# §14b: offline mode refuses the new page, with a clear error
# --------------------------------------------------------------------------- #

def test_offline_new_ui_raises_before_rendering():
    with pytest.raises(RuntimeError, match=re.escape(render_mod.OFFLINE_ERROR)):
        render_mod.render(NEW, VENDOR, cdn=False)
    with pytest.raises(RuntimeError, match=re.escape(render_mod.OFFLINE_ERROR)):
        render_mod.render(NEW, VENDOR)  # default is cdn=False


def test_offline_error_text_matches_plan_14b():
    assert render_mod.OFFLINE_ERROR == (
        "offline mode not supported by the new UI yet; elkjs vendoring pending"
    )


def test_cli_without_cdn_fails_cleanly(tmp_path):
    out = tmp_path / "refused.html"
    proc = run_cli("--snapshot", str(NEW_SNAPSHOT), "--out", str(out))
    assert proc.returncode != 0
    assert render_mod.OFFLINE_ERROR in proc.stderr
    assert not out.exists()  # nothing written before the refusal


def test_cli_with_cdn_renders_new_snapshot(tmp_path):
    out = tmp_path / "new.html"
    proc = run_cli(
        "--snapshot", str(NEW_SNAPSHOT), "--out", str(out), "--cdn",
        "--vendor", str(VENDOR),
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert src_urls(body) == [t.split('"')[1] for t in render_mod.cdn_script_tags()]
    assert len(body.lstrip()) > 0 and body.lstrip().startswith("<!doctype html")


def test_cli_default_vendor_dir_is_discovered():
    discovered = render_mod.default_vendor_dir()
    assert (discovered / "cytoscape.min.js").is_file(), discovered
