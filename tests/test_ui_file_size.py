"""800-line policy: page assets and operator-authorized checker boundaries."""

from __future__ import annotations

from pathlib import Path

import test_check_file_size as file_size

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"

BUDGET = 800


def test_page_assets_fit_line_budget():
    """Each *.js / *.css under src/plangraph/web/ must be <= 800 lines."""
    assets = sorted([*WEB.glob("*.js"), *WEB.glob("*.css")])
    assert assets, "no page assets found"
    over = []
    for path in assets:
        n = len(path.read_text(encoding="utf-8").splitlines())
        assert n > 0, f"{path.name} is empty"
        if n > BUDGET:
            over.append(f"{path.name} is {n} lines (budget {BUDGET})")
    assert not over, "; ".join(over)


class Operator800LineLimitTests(file_size.RepoFixture):
    def test_800_line_boundary(self):
        for count in (401, 800):
            self.file('new_module.py', count)
            self.assertPasses(self.check())
        self.file('new_module.py', 801)
        self.assertFails(self.check(), 'new_module.py')

    def test_update_prunes_entries_that_fit_the_new_cap(self):
        self.file('legacy.py', 800)
        self.baseline('legacy.py 900\nold.py 600\n')
        self.assertPasses(self.check('--update-baseline'))
        self.assertEqual(self.baseline_map(), {})
