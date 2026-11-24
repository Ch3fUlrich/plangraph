"""The UI job installs Chromium system packages before the timed browser tests, bounded and retried."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def browser_job():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    assert jobs["browser"]["name"] == "UI tests in headless Chromium"
    return jobs["browser"]


def browser_steps():
    return browser_job()["steps"]


def index_of(steps, needle):
    hits = [i for i, s in enumerate(steps) if needle in (s.get("with") or {}).get("command", "")]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


def test_browser_deps_are_installed_in_their_own_bounded_retried_step():
    steps = browser_steps()
    deps = steps[index_of(steps, "install-deps")]
    tests = steps[index_of(steps, "pytest -q tests/browser -m browser")]
    assert steps.index(deps) < steps.index(tests)
    assert "if" not in deps
    assert "install-deps chromium" in deps["with"]["command"]
    assert isinstance(deps.get("timeout-minutes"), int)
    assert deps["uses"] == "./.github/actions/bounded-step"
    # The step deadline sits below its step timeout, and the last try's failure fails the step.
    assert int(deps["with"]["seconds"]) < deps["timeout-minutes"] * 60
    command = deps["with"]["command"]
    assert re.search(r"seq 1 3|\{1\.\.3\}|1 2 3", command), command
    assert not re.search(r"seq 1 [4-9]|\{1\.\.[4-9]\}|1 2 3 4", command), command
    assert re.search(r"exit 1|\bfalse\b", command), command
    assert "|| true" not in command and "continue-on-error" not in deps


def test_browser_download_and_tests_are_not_relaxed_and_do_not_use_apt():
    steps = browser_steps()
    chromium = steps[index_of(steps, "playwright install chromium")]
    assert "--with-deps" not in chromium["with"]["command"]
    assert "continue-on-error" not in chromium
    tests = steps[index_of(steps, "pytest -q tests/browser -m browser")]
    assert tests["name"] == "browser tests"
    assert tests["with"]["seconds"] == 585
    assert tests["with"]["command"] == "python -m pytest -q tests/browser -m browser"
    assert "continue-on-error" not in tests
    # No apt work remains inside the timed test step or the browser download step.
    for step in (chromium, tests):
        assert "apt" not in step["with"]["command"]


def test_job_timeout_covers_every_step_running_to_its_own_timeout():
    job = browser_job()
    worst_case = sum(step["timeout-minutes"] for step in job["steps"] if "timeout-minutes" in step)
    assert job["timeout-minutes"] >= worst_case
