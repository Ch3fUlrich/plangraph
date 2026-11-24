"""Workflow wiring of the flake retry, the quarantine runs, the shard timings and the single pg-spawnd build.

Mutations killed: a retry that turns a red job green, a quarantine run that blocks or a blocking run that runs
quarantined tests, a pull request that reads its own ledger, a second pg-spawnd build, two shards that write one
timing file, a different pinned map per shard, a cache saved by a pull request, a required job made non-blocking,
and a retry that classifies more than fifty failures or keeps the shard environment.
"""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
JOBS = WORKFLOW["jobs"]
#: The only steps that may carry continue-on-error: classification, reporting and evidence, never a test run.
NONBLOCKING = {
    "Retry the failed tests once at the same SHA", "Upload the same-SHA retry logs and record",
    "Quarantined tests (reporting, never blocking)", "Retry the failed Rust tests once at the same SHA",
    "Quarantined Rust tests (reporting, never blocking)", "Carry ci-full evidence to a reworded head",
    "Quarantine ledger in the gate summary", "Publish the quarantine ledger", "Download shard timings",
    "Combine shard timings per suite", "Upload the combined shard timings", "Post ci-full evidence",
}


def steps(job):
    return JOBS[job]["steps"]


def named(job, name):
    return next(step for step in steps(job) if step.get("name") == name)


def index(job, name):
    return steps(job).index(named(job, name))


def test_only_classification_reporting_and_evidence_steps_are_non_blocking():
    marked = {step.get("name") for job in JOBS.values() for step in job["steps"] if step.get("continue-on-error")}
    assert marked <= NONBLOCKING, marked - NONBLOCKING
    for job in ("pytest", "pytest-postgres"):
        assert not named(job, "pytest" if job == "pytest" else "pytest (contract suite + keeper storage tests on Postgres)").get("continue-on-error")
    assert not named("rust", "cargo test").get("continue-on-error")
    assert "continue-on-error" not in JOBS["ci-gate"] and not named("ci-gate", "Gate").get("continue-on-error")


def test_the_gate_still_needs_every_job_it_needed_and_the_build():
    assert {"select", "scrub", "file-size", "new-code-lang", "changelog-docs", "pytest", "pytest-postgres", "rust",
            "rust-worker", "rust-spike", "rust-windows", "browser", "build-spawnd"} <= set(JOBS["ci-gate"]["needs"])


@pytest.mark.parametrize("job,test_step", [("pytest", "pytest"), ("pytest-postgres", "pytest (contract suite + keeper storage tests on Postgres)")])
def test_a_red_test_step_is_classified_by_a_retry_that_cannot_make_it_green(job, test_step):
    test = named(job, test_step)
    retry = named(job, "Retry the failed tests once at the same SHA")
    assert test["id"] == "pytest" and "tee" in test["with"]["command"]
    assert retry["continue-on-error"] is True and "failure()" in retry["if"] and "steps.pytest.outcome == 'failure'" in retry["if"]
    assert retry["uses"] == "./.github/actions/pytest-retry" and index(job, test_step) < index(job, retry["name"])
    # Nothing after the test step may rewrite its outcome: no step sets the job status.
    assert not any("exit 0" in str(step.get("run", "")) for step in steps(job)[index(job, test_step):])


@pytest.mark.parametrize("job,test_step", [("pytest", "pytest"), ("pytest-postgres", "pytest (contract suite + keeper storage tests on Postgres)")])
def test_the_ledger_is_pinned_before_the_blocking_run_and_quarantined_tests_run_only_in_the_reporting_step(job, test_step):
    pin = named(job, "Quarantine ledger and run timestamp")
    assert index(job, pin["name"]) < index(job, test_step)
    assert pin["with"]["as-of"] == "${{ needs.select.outputs.as-of }}"
    assert "github.event_name == 'pull_request'" in pin["with"]["base-sha"] and "base.sha" in pin["with"]["base-sha"]
    report = named(job, "Quarantined tests (reporting, never blocking)")
    assert report["env"]["PYTEST_QUARANTINE_MODE"] == "quarantine" and report["continue-on-error"] is True
    assert "matrix.shard == 0" in report["if"] and "active != '0'" in report["if"]
    for step in steps(job):
        if step is not report:
            assert "PYTEST_QUARANTINE_MODE" not in json.dumps(step), step.get("name")


def test_the_reporting_run_and_the_gate_publish_owner_and_expiry():
    ledger = yaml.safe_load((ROOT / ".github/actions/quarantine-ledger/action.yml").read_text())
    script = ledger["runs"]["steps"][0]["run"]
    assert 'quarantine --ledger "$ledger" --as-of "$AS_OF"' in script and "GITHUB_STEP_SUMMARY" in script
    assert "uses" in named("ci-gate", "Publish the quarantine ledger")
    assert "Quarantined Rust tests" in json.dumps(named("rust", "Quarantined Rust tests (reporting, never blocking)"))


def test_cargo_blocking_run_skips_the_active_entries_and_the_reporting_run_runs_them():
    blocking = named("rust", "cargo test")
    assert "--mode blocking" in blocking["run"] and "--skip" in blocking["run"] and "--no-fail-fast" in blocking["run"]
    report = named("rust", "Quarantined Rust tests (reporting, never blocking)")["run"]
    assert "--mode quarantine" in report and '-- --exact "$name"' in report
    retry = named("rust", "Retry the failed Rust tests once at the same SHA")
    assert retry["continue-on-error"] is True and "steps.cargo-test.outcome == 'failure'" in retry["if"]


def test_pg_spawnd_builds_once_per_run_and_the_shards_download_it():
    builders = [name for name, job in JOBS.items()
                if any(re.search(r"cargo build.*--release.*-p pg-spawnd", str(step.get("with", {}).get("command", "")) + str(step.get("run", "")))
                       for step in job["steps"])]
    assert builders == ["build-spawnd"]
    for job in ("pytest", "pytest-postgres"):
        assert "build-spawnd" in JOBS[job]["needs"]
        assert any(step.get("uses") == "./.github/actions/fetch-spawnd" for step in steps(job))
    assert any(step.get("uses", "").startswith("actions/upload-artifact@") and step["with"]["name"] == "pg-spawnd-bin"
               for step in steps("build-spawnd"))


def test_every_shard_reads_one_pinned_map_and_writes_its_own_timing_file():
    for job, test_step, suite in (("pytest", "pytest", "sqlite"), ("pytest-postgres", "pytest (contract suite + keeper storage tests on Postgres)", "postgres")):
        env = named(job, test_step)["env"]
        assert env["PYTEST_SHARD_DURATIONS_FILE"] == f".github/pytest-durations-{suite}.json"
        assert "matrix.shard" in env["PYTEST_SHARD_DURATIONS_OUT"] and suite in env["PYTEST_SHARD_DURATIONS_OUT"]
        upload = named(job, "Upload shard timings")
        assert upload["if"] == "${{ success() }}" and "matrix.shard" in upload["with"]["name"]
        assert not ROOT.joinpath(f".github/pytest-durations-{suite}.json").read_text().strip().startswith("[")
        json.loads(ROOT.joinpath(f".github/pytest-durations-{suite}.json").read_text())
    assert "combine-durations" in named("ci-gate", "Combine shard timings per suite")["run"]


def test_cargo_caches_are_saved_only_by_a_push_to_the_default_branch():
    saves = 0
    for name, job in JOBS.items():
        for step in job["steps"]:
            uses, path = str(step.get("uses", "")), json.dumps(step.get("with", {}).get("path", ""))
            if ".cargo" not in path:
                continue
            assert not uses.startswith("actions/cache@"), (name, step.get("name"))   # a plain cache saves on every run
            if uses.startswith("actions/cache/save@"):
                saves += 1
                assert "github.event_name == 'push'" in step["if"] and "default_branch" in step["if"], (name, step.get("name"))
            else:
                assert uses.startswith("actions/cache/restore@") and step["with"]["restore-keys"], (name, step.get("name"))
    assert saves == 5   # build-spawnd, rust, rust-windows, rust-worker, rust-spike


def test_select_publishes_one_run_timestamp_and_wires_the_reword_evidence():
    assert JOBS["select"]["outputs"]["as-of"] == "${{ steps.asof.outputs.as-of }}"
    select = named("select", "Select test suites")
    assert "--old-head" in select["run"] and "--old-run" in select["run"] and "BEFORE_SHA" in select["env"]
    assert "date -u" in named("select", "Run timestamp for the quarantine ledger")["run"]


# --- the composite actions' scripts, run against stubs ------------------------------------------------------------

def action_script(name):
    return yaml.safe_load((ROOT / f".github/actions/{name}/action.yml").read_text())["runs"]["steps"][-1]["run"] if name == "pytest-retry" \
        else yaml.safe_load((ROOT / f".github/actions/{name}/action.yml").read_text())["runs"]["steps"][0]["run"]


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run_script(script, cwd, env, tmp_path):
    out, step_env = tmp_path / "out", tmp_path / "env"
    summary = tmp_path / "summary"
    for file in (out, step_env, summary):
        file.touch()
    merged = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(step_env),
              "GITHUB_STEP_SUMMARY": str(summary), **env}
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=merged, capture_output=True, text=True), step_env, summary


def test_a_pull_request_reads_the_ledger_of_its_base_not_its_own(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],  # noqa: E731
                                    check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / ".github/pytest-quarantine.json").write_text('{"base": {}}')
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / ".github/pytest-quarantine.json").write_text('{"own": {}}')
    flake = tmp_path / "flake"
    stub(flake, 'cat "$3"; echo "active=0" >> "$GITHUB_OUTPUT"\n')
    script = action_script("quarantine-ledger")
    for base_sha, expected in ((base, '{"base": {}}'), ("", '{"own": {}}')):
        as_of = "2026-11-24T00:00:00+00:00"
        done, env_file, _ = run_script(script, repo, {"BASE_SHA": base_sha, "AS_OF": as_of, "FLAKE_BIN": str(flake)}, tmp_path)
        assert done.returncode == 0, done.stderr
        ledger = re.search(r"PYTEST_QUARANTINE_FILE=(.*)", env_file.read_text()).group(1)
        assert Path(ledger).read_text() == expected
        assert f"PYTEST_QUARANTINE_AS_OF={as_of}" in env_file.read_text()
    # A base commit without a ledger file is an absent ledger; an unknown commit is a failed lookup.
    git("rm", "-q", "-f", ".github/pytest-quarantine.json")
    git("commit", "-qm", "no ledger")
    bare = git("rev-parse", "HEAD")
    done, env_file, _ = run_script(script, repo, {"BASE_SHA": bare, "AS_OF": "x", "FLAKE_BIN": str(flake)}, tmp_path)
    assert done.returncode == 0 and Path(re.search(r"PYTEST_QUARANTINE_FILE=(.*)", env_file.read_text()).group(1)).read_text().strip() == "{}"
    unknown, _, _ = run_script(script, repo, {"BASE_SHA": "f" * 40, "AS_OF": "x", "FLAKE_BIN": str(flake)}, tmp_path)
    assert unknown.returncode != 0


def test_a_shallow_checkout_fetches_the_base_before_reading_its_ledger(tmp_path):
    origin = tmp_path / "origin"
    (origin / ".github").mkdir(parents=True)
    git = lambda root, *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],  # noqa: E731
                                          check=True, capture_output=True, text=True).stdout.strip()
    git(origin, "init", "-q", "-b", "main")
    (origin / ".github/pytest-quarantine.json").write_text('{"base": {}}')
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "base")
    base = git(origin, "rev-parse", "HEAD")
    (origin / ".github/pytest-quarantine.json").write_text('{"head": {}}')
    git(origin, "commit", "-qam", "head")
    git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{origin}", str(clone))
    assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True).returncode != 0
    flake = tmp_path / "flake"
    stub(flake, 'echo "active=0" >> "$GITHUB_OUTPUT"\n')
    done, env_file, _ = run_script(action_script("quarantine-ledger"), clone, {"BASE_SHA": base, "AS_OF": "2026-11-24T00:00:00+00:00", "FLAKE_BIN": str(flake)}, tmp_path)
    assert done.returncode == 0, done.stderr
    assert Path(re.search(r"PYTEST_QUARANTINE_FILE=(.*)", env_file.read_text()).group(1)).read_text() == '{"base": {}}'


def retry_fixture(tmp_path, failed):
    """A checkout with a stub ci-flake and a stub python that records how the retry ran pytest."""
    work = tmp_path / "work"
    (work / "rust/target/release").mkdir(parents=True)
    (tmp_path / "first.log").write_text("\n".join(f"FAILED tests/t.py::t{i} - x" for i in range(failed)) + "\n")
    stub(work / "rust/target/release/ci-flake",
         'case "$1" in failed-ids) grep -o "tests/t.py::t[0-9]*" "$2";; '
         'classify) echo "$*" >> "$RUNNER_TEMP/calls";; esac\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir / "python", 'echo "python $* SHARD=[$PYTEST_SHARD] OUT=[$PYTEST_SHARD_DURATIONS_OUT]" >> "$RUNNER_TEMP/calls"\n')
    return work, bin_dir


def test_the_retry_runs_the_failed_ids_once_without_the_shard_environment_and_classifies_both_logs(tmp_path):
    work, bin_dir = retry_fixture(tmp_path, 2)
    done, _, _ = run_script(action_script("pytest-retry"), work, {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "SUITE": "sqlite", "FIRST_LOG": str(tmp_path / "first.log"), "RUN_ID": "9",
        "RUN_ATTEMPT": "2", "RUNNER_ID": "r-1", "HEAD_SHA": "a" * 40, "PYTEST_SHARD": "1/4", "PYTEST_SHARD_DURATIONS_OUT": "/x/y.json"}, tmp_path)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "python -m pytest -q -rA -p no:cacheprovider -n 0 tests/t.py::t0 tests/t.py::t1 SHARD=[] OUT=[]"
    assert calls[1].startswith("classify --suite sqlite --runner r-1 --run-id 9 --attempt 2 --sha " + "a" * 40)
    assert "--first" in calls[1] and "first.log" in calls[1] and "--second" in calls[1] and "second.log" in calls[1]


def test_more_than_fifty_failures_are_classified_without_a_retry(tmp_path):
    work, bin_dir = retry_fixture(tmp_path, 51)
    done, _, _ = run_script(action_script("pytest-retry"), work, {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "SUITE": "sqlite", "FIRST_LOG": str(tmp_path / "first.log"), "RUN_ID": "9",
        "RUN_ATTEMPT": "1", "RUNNER_ID": "r", "HEAD_SHA": "b" * 40}, tmp_path)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 1 and calls[0].startswith("classify")


def cargo_fixture(tmp_path, entries, failing=""):
    target = tmp_path / "target"
    (target / "debug").mkdir(parents=True)
    stub(target / "debug/ci-flake", f'printf "%s\\n" {entries}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir / "cargo", f'echo "cargo $*" >> "$RUNNER_TEMP/calls"\ncase "$*" in *"{failing}"*) test -z "{failing}" || exit 1;; esac\n')
    return target, bin_dir


def test_a_cargo_exclusion_names_its_package_and_every_other_package_runs_in_one_workspace_run(tmp_path):
    target, bin_dir = cargo_fixture(tmp_path, "'pg-spawnd mod::case' 'pg-spawnd other::case' 'pg-keeper x::y'")
    script = named("rust", "cargo test")["run"]
    done, _, _ = run_script(script, tmp_path, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CARGO_TARGET_DIR": str(target),
                                               "GITHUB_WORKSPACE": str(tmp_path), "PYTEST_QUARANTINE_FILE": "l", "PYTEST_QUARANTINE_AS_OF": "t"}, tmp_path)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0].startswith("cargo test --no-fail-fast --workspace") and "--exclude pg-spawnd" in calls[0] and "--exclude pg-keeper" in calls[0]
    assert "cargo test --no-fail-fast -p pg-spawnd -- --exact --skip mod::case --skip other::case" in calls
    assert "cargo test --no-fail-fast -p pg-keeper -- --exact --skip x::y" in calls
    assert (tmp_path / "cargo-first.log").is_file()


def test_a_failing_package_run_fails_the_step_and_no_ledger_entry_runs_the_plain_workspace(tmp_path):
    target, bin_dir = cargo_fixture(tmp_path, "'pg-spawnd mod::case'", failing="-p pg-spawnd")
    script = named("rust", "cargo test")["run"]
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CARGO_TARGET_DIR": str(target), "GITHUB_WORKSPACE": str(tmp_path),
           "PYTEST_QUARANTINE_FILE": "l", "PYTEST_QUARANTINE_AS_OF": "t"}
    done, _, _ = run_script(script, tmp_path, env, tmp_path)
    assert done.returncode == 1
    plain_target, plain_bin = cargo_fixture(tmp_path / "plain", "")
    (tmp_path / "plain").mkdir(exist_ok=True)
    (tmp_path / "plain" / "calls").unlink(missing_ok=True)
    done, _, _ = run_script(script, tmp_path / "plain", env | {"PATH": f"{plain_bin}:{os.environ['PATH']}", "CARGO_TARGET_DIR": str(plain_target),
                                                              "RUNNER_TEMP": str(tmp_path / "plain")}, tmp_path / "plain")
    assert done.returncode == 0
    assert (tmp_path / "plain" / "calls").read_text().strip() == "cargo test --no-fail-fast --workspace"


@pytest.mark.parametrize("job", ["pytest", "pytest-postgres"])
def test_one_classification_retry_across_both_paths_and_every_pytest_step_is_bounded(job):
    retry = named(job, "Retry the failed tests once at the same SHA")
    # The affected path (a pull request without the full run) already reruns its failures with -n0 and the first worker count.
    assert "!(github.event_name == 'pull_request' && needs.select.outputs.full != 'true')" in retry["if"]
    report = named(job, "Quarantined tests (reporting, never blocking)")
    assert report["uses"] == "./.github/actions/bounded-step" and 0 < int(report["with"]["seconds"]) < report["timeout-minutes"] * 60
    assert report["timeout-minutes"] < JOBS[job]["timeout-minutes"]


def test_the_timing_merge_is_not_a_pytest_step_and_the_reuse_keeps_the_go_verifier_and_ci_full_honest():
    assert "pytest" not in named("ci-gate", "Combine shard timings per suite")["run"]
    go = named("ci-gate", "Verify computed GO")
    assert go["if"] == "${{ success() && github.event_name == 'pull_request' && needs.select.outputs.full == 'true' }}"
    assert "tested != 'true'" in named("ci-gate", "Post ci-full evidence")["if"]
    assert "--old-jobs" in named("select", "Select test suites")["run"] and "attempts/" in named("select", "Select test suites")["run"]
