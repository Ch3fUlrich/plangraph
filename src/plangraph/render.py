"""Render one self-contained, read-only HTML page from a plan snapshot.

Legacy snapshots inline three vendored MIT libraries (cytoscape, dagre,
cytoscape-dagre) and the snapshot JSON, so the page makes no network requests
and can be published as a single artifact. New-UI snapshots (items carrying
``phase``/``loop`` or a top-level ``questions``/``decisions`` list) are
**CDN-only in v1** (UI-PLAN §14b): ``cdn=False`` raises a clear
``RuntimeError`` until elkjs is vendored, while ``cdn=True`` references
cytoscape 3.34.0 (cdnjs) and elkjs 0.9.3 (jsDelivr) — both ``defer`` — and
inlines neither.

Display state (``ready`` / ``blocked``) and the critical path are computed in the
page's own JavaScript from the snapshot, exactly as the SPEC requires. This module
deliberately does not import :mod:`plangraph.views` — the two modules are written in
parallel and the page must stand alone.

Public API
----------
``render(snapshot, vendor_dir, *, title=None, cdn=False) -> str``
    The SPEC entry point. Returns the whole HTML document as one string.
``cdn_script_tags() -> list[str]``
    The exact ``<script defer src=…>`` tags used when ``cdn=True``.
``main(argv=None) -> int``
    CLI: ``python -m plangraph.render --snapshot S.json --out plan.html [--cdn]``.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from typing import Any

__all__ = ["render", "main", "default_vendor_dir", "cdn_script_tags"]

#: Assets (template, CSS, JS) shipped next to this module.
ASSETS_DIR = Path(__file__).resolve().parent / "web"

#: Page code concatenated, in load order, into the one inline ``<script>``
#: (UI-PLAN: ES5/ES2015, no modules, one namespace). ``page_model/store/search``
#: are the pure PlanUI core of T1; they have no load-time side effects.
JS_FILES = (
    "page.js",
    "page_ui.js",
    "page_ui_filters.js",
    "page_details.js",
    "page_model.js",
    "page_store.js",
    "page_search.js",
    "page_side.js",
    "page_side_tables.js",
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
    "page_table_view.js",
    "page_qa.js",
    "page_pages.js",
    "page_activity.js",
    "page_boot.js",  # T2: boot must be last — it wires the components it finds.
)

#: Stylesheets concatenated into ``@@PAGE_CSS@@``, in load order.
CSS_FILES = ("page.css", "page_base.css", "page_graph.css")

#: Vendored libraries for the legacy OFFLINE mode, in load order (dagre before
#: cytoscape-dagre). Kept inline-able because tests/test_render.py requires the
#: offline page to stay fully self-contained; the CDN mode no longer uses them.
VENDOR_LIBS = ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js")

#: cdnjs coordinates for cdn mode: cytoscape only (UI-PLAN §14 — dagre and
#: cytoscape-dagre are removed vs the legacy page). URL: ``<base>/<lib>/<ver>/<file>``.
CDN_BASE = "https://cdnjs.cloudflare.com/ajax/libs"
CDN_LIBS = (
    ("cytoscape", "3.34.0", "cytoscape.min.js"),
)

#: elkjs (EPL-2.0) from jsDelivr's npm mirror — the new layout engine (§14a).
CDN_ELK_URL = "https://cdn.jsdelivr.net/npm/elkjs@0.9.3/lib/elk.bundled.js"

#: The §14b v1 error: offline (inline) mode for new-UI snapshots stays off until
#: elkjs vendoring (licence EPL-2.0) is accepted or ordered by the router.
OFFLINE_ERROR = "offline mode not supported by the new UI yet; elkjs vendoring pending"


def default_vendor_dir() -> Path:
    """Locate the repository ``vendor/`` directory.

    Honours ``$PLANGRAPH_VENDOR``; otherwise walks up from this file looking for a
    ``vendor/cytoscape.min.js``. Falls back to ``./vendor`` for source checkouts.
    """
    override = os.environ.get("PLANGRAPH_VENDOR")
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vendor"
        if (candidate / "cytoscape.min.js").is_file():
            return candidate
    return Path("vendor")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _safe_json(obj: Any) -> str:
    """Serialise *obj* so it is safe inside a ``<script type="application/json">``.

    Escaping every ``<`` as ``\\u003c`` (and ``>``/``&`` for good measure) removes
    any chance of a value containing ``</script>`` or ``<!--`` terminating or
    corrupting the surrounding element. ``JSON.parse`` restores the characters.
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _default_title(items: list) -> str:
    n = len(items)
    return "Plan graph" if n == 0 else f"Plan graph ({n} items)"


def _new_ui(snapshot: dict) -> bool:
    """True when *snapshot* carries the new-UI data contract (§14b/§15).

    The new page consumes ``phase``/``loop`` per item and the ``questions`` /
    ``decisions`` lists; a snapshot carrying none of them (the legacy page
    fixture, the current keeper exporter output) is rendered on the unchanged
    offline path. Snapshots that ARE new-UI are CDN-only until elkjs is
    vendored — ``render(..., cdn=False)`` raises :data:`OFFLINE_ERROR`.
    """
    if snapshot.get("questions") is not None or snapshot.get("decisions") is not None:
        return True
    items = snapshot.get("items")
    if isinstance(items, list):
        return any(
            isinstance(i, dict) and ("phase" in i or "loop" in i) for i in items
        )
    return False


def cdn_script_tags() -> list[str]:
    """The exact ``<script defer src=…>`` tags used when ``cdn=True`` (§14).

    cytoscape 3.34.0 from cdnjs first (it draws nothing until elkjs lands, and
    both are `defer` so order of execution follows document order), then elkjs
    0.9.3 from jsDelivr. dagre / cytoscape-dagre tags are gone with the
    layout-engine swap.
    """
    tags = [
        f'<script defer src="{CDN_BASE}/{library}/{version}/{filename}"></script>'
        for library, version, filename in CDN_LIBS
    ]
    tags.append(f'<script defer src="{CDN_ELK_URL}"></script>')
    return tags


def _vendor_scripts(vendor_dir: Path, cdn: bool) -> str:
    """The one block substituted for ``@@VENDOR_SCRIPTS@@``.

    Inline mode reads and joins the vendored files inside a single ``<script>``
    (missing files are skipped); CDN mode emits one deferred tag per library.
    """
    if cdn:
        return "\n".join(cdn_script_tags())
    vendor_blocks = []
    for name in VENDOR_LIBS:
        path = vendor_dir / name
        if path.is_file():
            vendor_blocks.append(_read(path))
    vendor_js = "\n;\n".join(vendor_blocks)
    return f"<script>\n{vendor_js}\n</script>"



def _strip_line_comments(js: str) -> str:
    """Drop whole-line ``//`` comments and block comments that start a line.

    Keeps the page under its size budget (UI-PLAN 12) without a minifier:
    only lines whose first non-blank characters open a comment are touched,
    and code after a closing ``*/`` on the same line is kept.
    """
    out, in_block = [], False
    for line in js.split("\n"):
        text = line.strip()
        if in_block:
            if "*/" in text:
                in_block = False
                rest = text.split("*/", 1)[1].strip()
                if rest:
                    out.append(rest)
            continue
        if text.startswith("//"):
            continue
        if text.startswith("/*"):
            if "*/" not in text:
                in_block = True
            else:
                rest = text.split("*/", 1)[1].strip()
                if rest:
                    out.append(rest)
            continue
        out.append(line)
    return "\n".join(out)

def render(
    snapshot: dict,
    vendor_dir: Path,
    *,
    title: str | None = None,
    cdn: bool = False,
) -> str:
    """Return the complete HTML page for *snapshot* as a single string.

    Offline (``cdn=False``): legacy snapshots inline the three files named in
    :data:`VENDOR_LIBS` (a missing file is skipped rather than fatal); new-UI
    snapshots raise :data:`OFFLINE_ERROR` (UI-PLAN §14b). With ``cdn=True``
    no vendor file is read — cytoscape/elkjs come from the CDN instead.
    """
    if not isinstance(snapshot, dict):
        snapshot = {}
    items = snapshot.get("items")
    if not isinstance(items, list):
        items = []
    # Always give the page JS an `items` array, even for a malformed/empty snapshot.
    safe_snapshot = dict(snapshot)
    safe_snapshot["items"] = items

    if not cdn and _new_ui(safe_snapshot):  # §14b: the new page is --cdn only (v1)
        raise RuntimeError(OFFLINE_ERROR)

    vendor_dir = Path(vendor_dir)
    vendor_scripts = _vendor_scripts(vendor_dir, bool(cdn))

    page_js = _strip_line_comments("\n;\n".join(_read(ASSETS_DIR / name) for name in JS_FILES))
    page_css = "\n".join(_read(ASSETS_DIR / name) for name in CSS_FILES)

    doc = _read(ASSETS_DIR / "page_template.html")
    replacements = {
        "@@PAGE_TITLE@@": html.escape(title or _default_title(items), quote=True),
        "@@PAGE_CSS@@": page_css,
        "@@VENDOR_SCRIPTS@@": vendor_scripts,
        "@@PAGE_JS@@": page_js,
        "@@SNAPSHOT_JSON@@": _safe_json(safe_snapshot),
    }
    for token, value in replacements.items():
        doc = doc.replace(token, value)
    return doc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="plangraph.render",
        description="Render a plan snapshot to one self-contained HTML page.",
    )
    parser.add_argument("--snapshot", required=True, help="path to the snapshot JSON")
    parser.add_argument("--out", required=True, help="path of the HTML file to write")
    parser.add_argument("--vendor", default=None, help="directory holding the vendored JS (default: auto)")
    parser.add_argument("--title", default=None, help="page title (default: derived from the item count)")
    parser.add_argument(
        "--cdn",
        action="store_true",
        help="reference cytoscape/elkjs from the CDN instead of inlining (required for new-UI snapshots)",
    )
    args = parser.parse_args(argv)

    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    vendor_dir = Path(args.vendor) if args.vendor else default_vendor_dir()
    document = render(snapshot, vendor_dir, title=args.title, cdn=args.cdn)
    out_path = Path(args.out)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
