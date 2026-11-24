"""The UI job installs Chromium system packages before the timed browser tests: cached, bounded, retried."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TESTS_COMMAND = "python -m pytest -q tests/browser -m browser"


def browser_job():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    assert jobs["browser"]["name"] == "UI tests in headless Chromium"
    return jobs["browser"]


def step_named(name):
    hits = [s for s in browser_job()["steps"] if s.get("name") == name]
    assert len(hits) == 1, (name, hits)
    return hits[0]


def position(name):
    return browser_job()["steps"].index(step_named(name))


def command(step):
    return step["with"]["command"]


def test_package_list_comes_from_playwright_dry_run_before_the_cache_restore():
    listing = step_named("List Chromium system dependencies")
    assert "install-deps --dry-run chromium" in command(listing)
    assert "apt-get update" in command(listing) and "Acquire::Retries=3" in command(listing)
    assert position("Install test deps") < position("List Chromium system dependencies")
    assert position("List Chromium system dependencies") < position("Restore apt package cache")


def test_apt_archives_are_cached_keyed_on_the_package_list_and_saved_after_the_install():
    restore = step_named("Restore apt package cache")
    save = step_named("Save apt package cache")
    for step in (restore, save):
        assert step["with"]["key"] == step_named("Restore apt package cache")["with"]["key"]
        assert "hashFiles('.ci-apt-packages.txt')" in step["with"]["key"]
        assert "apt-archives" in step["with"]["path"]
    assert restore["id"] == "apt-cache" and restore["uses"].startswith("actions/cache/restore@")
    assert save["uses"].startswith("actions/cache/save@")
    assert "cache-hit" in save["if"]
    assert position("Restore apt package cache") < position("Install Chromium system dependencies")
    assert position("Install Chromium system dependencies") < position("Save apt package cache")
    # The archive directory the cache stores is the one apt downloads into.
    assert "Dir::Cache::archives" in command(step_named("Install Chromium system dependencies"))


def test_browser_deps_are_installed_by_apt_in_a_bounded_retried_step_that_fails_the_job():
    deps = step_named("Install Chromium system dependencies")
    assert "if" not in deps and "continue-on-error" not in deps
    assert deps["uses"] == "./.github/actions/bounded-step"
    text = command(deps)
    assert "apt-get install" in text
    # Playwright's own installer is not used: it left apt/dpkg state behind when killed.
    assert "install-deps chromium" not in text
    # The whole process group is killed as root, the dpkg state repaired before every try.
    assert re.search(r"sudo (env \S+ )?timeout -k \d+s", text), text
    assert "dpkg --configure -a" in text
    assert "DPkg::Lock::Timeout=120" in text and "Acquire::Retries=3" in text
    assert "for try in 1 2 3" in text and not re.search(r"for try in .*\b4\b", text)
    assert re.search(r"exit 1\s*$", text.strip()), text
    assert "|| true" not in text
    # Try 1 has time for a slow mirror (32 MB at ~100 kB/s), and every limit fits the step.
    limits = [int(n) for n in re.search(r"limits=\(([\d ]+)\)", text).group(1).split()]
    assert len(limits) == 3 and limits[0] >= 330
    assert int(deps["with"]["seconds"]) > sum(limits)
    assert int(deps["with"]["seconds"]) < deps["timeout-minutes"] * 60
    assert position("Install Chromium system dependencies") < position("Install Chromium")


def test_browser_download_and_tests_are_not_relaxed_and_do_not_use_apt():
    chromium = step_named("Install Chromium")
    assert command(chromium) == "python -m playwright install chromium"
    assert "continue-on-error" not in chromium
    tests = step_named("browser tests")
    assert tests["with"]["seconds"] == 585
    assert command(tests) == TESTS_COMMAND
    assert "continue-on-error" not in tests and "if" not in tests
    assert position("Install Chromium") < position("browser tests")
    for step in (chromium, tests):
        assert "apt" not in command(step)


def test_job_timeout_covers_every_step_running_to_its_own_timeout():
    job = browser_job()
    worst_case = sum(step["timeout-minutes"] for step in job["steps"] if "timeout-minutes" in step)
    assert job["timeout-minutes"] >= worst_case
    # Every step has a timeout, so the sum is the whole worst case.
    assert all("timeout-minutes" in step for step in job["steps"])


@pytest.mark.skipif(shutil.which("awk") is None, reason="awk missing")
def test_package_list_parser_reads_playwrights_dry_run_output(tmp_path):
    text = command(step_named("List Chromium system dependencies"))
    program = re.search(r"awk '([^']+)'", text).group(1)
    dry_run = tmp_path / "dry-run.txt"
    dry_run.write_text("Missing system dependencies (3):\n  libgbm1\n  xvfb\n  fonts-liberation\nnoise\n")
    out = subprocess.run(["awk", program, str(dry_run)], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["libgbm1", "xvfb", "fonts-liberation"]
    dry_run.write_text("Everything is installed\n")
    assert subprocess.run(["awk", program, str(dry_run)], capture_output=True, text=True, check=True).stdout == ""
