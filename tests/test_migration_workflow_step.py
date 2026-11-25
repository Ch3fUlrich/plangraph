"""The required ci-gate job runs `land-train require-migration`, and cannot be made to skip or swallow it."""
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml"


def _step():
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["ci-gate"]
    steps = [s for s in job["steps"] if "require-migration" in s.get("run", "")]
    assert len(steps) == 1
    return job, steps[0]


def test_the_step_runs_the_command_on_the_merge_base_and_the_pull_request_body():
    job, step = _step()
    run = step["run"]
    assert "land-train require-migration" in run
    assert 'git merge-base "$PR_BASE" "$PR_HEAD"' in run and '--head "$PR_HEAD"' in run
    assert "--body-file" in run and step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
    # The body reaches the script through the environment, never interpolated into the shell text.
    assert "github.event.pull_request.body" not in run


def test_the_step_is_blocking_and_runs_where_go_verify_runs():
    job, step = _step()
    go = next(s for s in job["steps"] if s.get("name") == "Verify computed GO")
    assert step["if"] == go["if"]
    assert "continue-on-error" not in step
    for swallow in ("|| true", "|| :", "set +e"):
        assert swallow not in step["run"]
    assert step["run"].lstrip().startswith("set -euo pipefail")
    assert job["steps"].index(step) > job["steps"].index(go)
