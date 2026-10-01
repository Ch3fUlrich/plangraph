"""Render one self-contained, read-only HTML page from a plan snapshot.

The page inlines three vendored MIT libraries (cytoscape, dagre, cytoscape-dagre)
and the snapshot JSON, so it makes no network requests and can be published as a
single artifact and opened from disk or a phone.

Display state (``ready`` / ``blocked``) and the critical path are computed in the
page's own JavaScript from the snapshot, exactly as the SPEC requires. This module
deliberately does not import :mod:`plangraph.views` — the two modules are written in
parallel and the page must stand alone.

Public API
----------
``render(snapshot, vendor_dir, *, title=None, cdn=False) -> str``
    The SPEC entry point. Returns the whole HTML document as one string. With
    ``cdn=True`` the three vendor libraries are referenced from a CDN instead of
    inlined (the snapshot data and the page's own JS are still inlined).

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

#: Vendored libraries, in load order (dagre before cytoscape-dagre).
VENDOR_LIBS = ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js")

#: cdnjs coordinates, in load order, for the two libraries cdnjs hosts. The URL
#: form is ``<base>/<library>/<version>/<file>``.
CDN_BASE = "https://cdnjs.cloudflare.com/ajax/libs"
CDN_LIBS = (
    ("cytoscape", "3.34.0", "cytoscape.min.js"),
    ("dagre", "0.8.5", "dagre.min.js"),
)

#: cdnjs does not host ``cytoscape-dagre`` (the cdnjs path 404s, checked
#: 2026-10-01); jsDelivr serves the npm package directly. Loaded last, after
#: cytoscape and dagre.
CDN_DAGRE_URL = (
    "https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.min.js"
)


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


def cdn_script_tags() -> list[str]:
    """The exact ``<script src=…>`` tags used when ``cdn=True``.

    cytoscape and dagre come from cdnjs; cytoscape-dagre, which cdnjs does not
    host, comes from jsDelivr and is loaded last so it can see both globals.
    """
    tags = [
        f'<script src="{CDN_BASE}/{library}/{version}/{filename}"></script>'
        for library, version, filename in CDN_LIBS
    ]
    tags.append(f'<script src="{CDN_DAGRE_URL}"></script>')
    return tags


def _vendor_scripts(vendor_dir: Path, cdn: bool) -> str:
    """The one block substituted for ``@@VENDOR_SCRIPTS@@``.

    Inline mode reads and joins the vendored files inside a single ``<script>``
    (missing files are skipped); CDN mode emits one external tag per library.
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


def render(
    snapshot: dict,
    vendor_dir: Path,
    *,
    title: str | None = None,
    cdn: bool = False,
) -> str:
    """Return the complete HTML page for *snapshot* as a single string.

    ``vendor_dir`` must contain the three files named in :data:`VENDOR_LIBS`; a
    missing file is skipped rather than fatal so the page still renders. With
    ``cdn=True`` those files are not read at all — the page references
    cytoscape/dagre on cdnjs and cytoscape-dagre on jsDelivr instead.
    """
    if not isinstance(snapshot, dict):
        snapshot = {}
    items = snapshot.get("items")
    if not isinstance(items, list):
        items = []
    # Always give the page JS an `items` array, even for a malformed/empty snapshot.
    safe_snapshot = dict(snapshot)
    safe_snapshot["items"] = items

    vendor_dir = Path(vendor_dir)
    vendor_scripts = _vendor_scripts(vendor_dir, bool(cdn))

    page_js = "\n;\n".join(
        _read(ASSETS_DIR / name)
        for name in ("page.js", "page_ui.js", "page_details.js")
    )

    doc = _read(ASSETS_DIR / "page_template.html")
    replacements = {
        "@@PAGE_TITLE@@": html.escape(title or _default_title(items), quote=True),
        "@@PAGE_CSS@@": _read(ASSETS_DIR / "page.css"),
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
        help="reference cytoscape/dagre/cytoscape-dagre from cdnjs instead of inlining them",
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
