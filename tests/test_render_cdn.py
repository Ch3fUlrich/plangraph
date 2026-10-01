"""Tests for the CDN rendering mode (Task A3).

``cdn=True`` must reference cytoscape and dagre from cdnjs and cytoscape-dagre
from jsDelivr, inline none of their source, and use no other external host; the
snapshot data and the page's own code stay inline, and the default
(``cdn=False``) output is unchanged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENDOR = ROOT / "vendor"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "page_snapshot.json"

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402

SNAPSHOT = json.loads(FIXTURE.read_text(encoding="utf-8"))

CDN_URLS = (
    "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.34.0/cytoscape.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js",
    "https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.min.js",
)

#: The only external hosts the CDN mode is allowed to reference.
ALLOWED_HOSTS = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net")

VENDOR_FILES = ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js")


def html_for(cdn: bool) -> str:
    return render_mod.render(SNAPSHOT, VENDOR, cdn=cdn)


# --------------------------------------------------------------------------- #
# default mode is unchanged
# --------------------------------------------------------------------------- #

def test_default_is_unchanged_and_has_no_cdn():
    explicit = render_mod.render(SNAPSHOT, VENDOR, cdn=False)
    implicit = render_mod.render(SNAPSHOT, VENDOR)
    assert explicit == implicit
    assert "cdnjs.cloudflare.com" not in explicit
    for name in VENDOR_FILES:
        assert (VENDOR / name).read_text(encoding="utf-8") in explicit
    # still no external script tag
    for tag in re.findall(r"<script\b[^>]*>", explicit, re.IGNORECASE):
        assert "src" not in tag.lower(), tag


# --------------------------------------------------------------------------- #
# cdn mode
# --------------------------------------------------------------------------- #

def test_cdn_references_the_exact_cdn_urls():
    html = html_for(cdn=True)
    for url in CDN_URLS:
        assert f'src="{url}"' in html, url
    assert render_mod.cdn_script_tags() == [
        f'<script src="{url}"></script>' for url in CDN_URLS
    ]


def test_cdn_references_only_allowed_hosts():
    html = html_for(cdn=True)
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html, re.IGNORECASE)
    assert len(srcs) == 3
    for url in srcs:
        assert urllib.parse.urlsplit(url).netloc in ALLOWED_HOSTS, url
    # no other external reference to any host outside the allow-list anywhere
    for url in re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html,
                          re.IGNORECASE):
        assert urllib.parse.urlsplit(url).netloc in ALLOWED_HOSTS, url


def test_cdn_does_not_inline_any_vendor_text():
    html = html_for(cdn=True)
    for name in VENDOR_FILES:
        body = (VENDOR / name).read_text(encoding="utf-8")
        # check a distinctive slice, so the assertion is cheap and meaningful
        probe = body[:2000]
        assert probe not in html, f"{name} appears inlined in cdn mode"


def test_cdn_still_inlines_data_and_page_code():
    html = html_for(cdn=True)
    # every item key travels with the page
    for item in SNAPSHOT["items"]:
        assert item["key"] in html
    # page JS is still embedded verbatim
    assert (SRC / "plangraph" / "web" / "page.js").read_text(encoding="utf-8") in html
    assert '<script type="application/json" id="snapshot-data">' in html


def test_cdn_does_not_need_a_vendor_dir(tmp_path):
    html = render_mod.render(SNAPSHOT, tmp_path / "does-not-exist", cdn=True)
    for url in CDN_URLS:
        assert url in html
    assert html.lstrip().lower().startswith("<!doctype html")


def test_cdn_cli_flag(tmp_path):
    snap_path = tmp_path / "s.json"
    out_path = tmp_path / "plan.html"
    snap_path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run(
        [sys.executable, "-m", "plangraph.render", "--snapshot", str(snap_path),
         "--out", str(out_path), "--cdn"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    written = out_path.read_text(encoding="utf-8")
    for url in CDN_URLS:
        assert url in written
    assert (VENDOR / "cytoscape.min.js").read_text(encoding="utf-8")[:2000] not in written
