"""W8: every page asset (src/plangraph/web/*.js and *.css) is at most 400 lines."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"

BUDGET = 400


def test_page_assets_fit_line_budget():
    """Each *.js / *.css under src/plangraph/web/ must be <= 400 lines."""
    assets = sorted([*WEB.glob("*.js"), *WEB.glob("*.css")])
    assert assets, "no page assets found"
    over = []
    for path in assets:
        n = len(path.read_text(encoding="utf-8").splitlines())
        assert n > 0, f"{path.name} is empty"
        if n > BUDGET:
            over.append(f"{path.name} is {n} lines (budget {BUDGET})")
    assert not over, "; ".join(over)
