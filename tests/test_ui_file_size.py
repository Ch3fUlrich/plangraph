"""800-line policy: page assets and operator-authorized checker boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("base", ["origin/main", "f" * 40])
def test_fixture_repos_ignore_parent_policy_base(base):
    nodes = [
        "tests/test_check_file_size.py::LineLimitTests::test_a_800_line_file_passes",
        "tests/test_ui_file_size.py::Operator800LineLimitTests::test_800_line_boundary",
        "tests/test_hooks_prepush.py::test_hook_passes_a_clean_push",
        "tests/test_hooks_prepush_refs.py::test_hook_passes_safe_ref_updates[True]",
    ]
    env = dict(os.environ, FILESIZE_REQUIRE_BASE=base, NEWCODE_BASE=base, CHANGELOG_BASE=base,
               CLASS_SEARCH_BASE=base, PYTEST_ADDOPTS="", PYTEST_SHARD="", PYTEST_SHARD_DURATIONS_OUT="")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", *nodes], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "4 passed" in result.stdout, result.stdout + result.stderr
