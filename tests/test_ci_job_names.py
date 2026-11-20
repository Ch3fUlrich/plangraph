"""Every tests.yml job has a distinct display name, and the name readers match it."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import merge_batch_land as land  # noqa: E402

JOBS = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]


def _static(name: str) -> str:
    return re.sub(r"\$\{\{.*?\}\}", "N", name)


def test_every_job_has_a_display_name():
    assert [job for job, body in JOBS.items() if not str(body.get("name", "")).strip()] == []


def test_display_names_are_unique():
    names = [_static(str(body["name"])) for body in JOBS.values()]
    assert len(names) == len(set(names))


def test_ci_gate_keeps_its_branch_protection_context():
    assert JOBS["ci-gate"]["name"] == "ci-gate"


def test_merge_batch_land_names_match_the_workflow():
    expected = {job: body["name"] for job, body in JOBS.items() if job not in land.SHARD_DISPLAY}
    assert land.JOB_DISPLAY == expected
    for suite, prefix in land.SHARD_DISPLAY.items():
        assert JOBS[suite]["name"].startswith(prefix + " (shard ")


def test_job_readers_accept_new_and_old_names():
    new = {land.JOB_DISPLAY[k] for k in land.JOB_DISPLAY}
    assert all(land.job_named(new, k) for k in land.JOB_DISPLAY)
    assert all(land.job_named({k}, k) for k in land.JOB_DISPLAY)
    shards = {f"{land.SHARD_DISPLAY['pytest']} (shard {i} of 4)" for i in (1, 2, 3, 4)}
    assert all(land.shard_named(shards, "pytest", i) for i in range(3))
    assert not land.shard_named(shards, "pytest-postgres", 0)
    assert land.shard_named({"pytest-postgres (1)"}, "pytest-postgres", 1)
    assert not land.shard_named({f"{land.SHARD_DISPLAY['pytest']} (shard 2 of 3)"}, "pytest", 0)
