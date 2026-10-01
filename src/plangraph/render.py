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
``render(snapshot, vendor_dir, *, title=None) -> str``
    The SPEC entry point. Returns the whole HTML document as one string.

``main(argv=None) -> int``
    CLI: ``python -m plangraph.render --snapshot S.json --out plan.html``.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from typing import Any

__all__ = ["render", "main", "default_vendor_dir"]

#: Assets (template, CSS, JS) shipped next to this module.
ASSETS_DIR = Path(__file__).resolve().parent / "web"

#: Vendored libraries, in load order (dagre before cytoscape-dagre).
VENDOR_LIBS = ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js")


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


def render(snapshot: dict, vendor_dir: Path, *, title: str | None = None) -> str:
    """Return the complete HTML page for *snapshot* as a single string.

    ``vendor_dir`` must contain the three files named in :data:`VENDOR_LIBS`; a
    missing file is skipped rather than fatal so the page still renders.
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
    vendor_blocks = []
    for name in VENDOR_LIBS:
        path = vendor_dir / name
        if path.is_file():
            vendor_blocks.append(_read(path))
    vendor_js = "\n;\n".join(vendor_blocks)

    page_js = "\n;\n".join(
        _read(ASSETS_DIR / name)
        for name in ("page.js", "page_ui.js", "page_details.js")
    )

    doc = _read(ASSETS_DIR / "page_template.html")
    replacements = {
        "@@PAGE_TITLE@@": html.escape(title or _default_title(items), quote=True),
        "@@PAGE_CSS@@": _read(ASSETS_DIR / "page.css"),
        "@@VENDOR_JS@@": vendor_js,
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
    args = parser.parse_args(argv)

    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    vendor_dir = Path(args.vendor) if args.vendor else default_vendor_dir()
    document = render(snapshot, vendor_dir, title=args.title)
    out_path = Path(args.out)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
