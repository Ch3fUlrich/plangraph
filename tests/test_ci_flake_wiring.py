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
from _split_ci_environment import python_test_env

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
JOBS = WORKFLOW["jobs"]
#: The only steps that may carry continue-on-error: classification, reporting and evidence, never a test run.
NONBLOCKING = {
    "Retry the failed tests once at the same SHA", "Upload the same-SHA retry logs and record",
    "Quarantined tests (reporting, never blocking)", "Retry the failed Rust tests once at the same SHA",
    "Quarantined Rust tests (reporting, never blocking)", "Quarantined Rust tests on Windows (reporting, never blocking)",
    "Retry the failed pg-spawnd tests once at the same SHA", "Retry the failed pg-worker-core tests once at the same SHA",
    "Upload the same-SHA retry logs and record (Windows)", "Retry the failed worker tests once at the same SHA",
    "Quarantined worker tests (reporting, never blocking)", "Retry the failed native writer tests once at the same SHA",
    "Upload the native writer retry logs and record", "Carry ci-full evidence to a reworded head",
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
def test_every_red_pytest_path_gets_one_classification_retry_and_every_pytest_step_is_bounded(job):
    retry = named(job, "Retry the failed tests once at the same SHA")
    # The affected pull request path is classified too: the retry keeps the first and the retry log with the tested SHA.
    assert retry["if"] == "${{ failure() && steps.pytest.outcome == 'failure' }}"
    assert "first.log" in yaml.safe_load((ROOT / ".github/actions/pytest-retry/action.yml").read_text())["runs"]["steps"][0]["run"]
    report = named(job, "Quarantined tests (reporting, never blocking)")
    assert report["uses"] == "./.github/actions/bounded-step" and 0 < int(report["with"]["seconds"]) < report["timeout-minutes"] * 60
    assert report["timeout-minutes"] < JOBS[job]["timeout-minutes"]


def test_the_timing_merge_is_not_a_pytest_step_and_the_reuse_keeps_the_go_verifier_and_ci_full_honest():
    assert "pytest" not in named("ci-gate", "Combine shard timings per suite")["run"]
    go = named("ci-gate", "Verify computed GO")
    assert go["if"] == "${{ success() && github.event_name == 'pull_request' && needs.select.outputs.full == 'true' }}"
    assert "tested != 'true'" in named("ci-gate", "Post ci-full evidence")["if"]
    assert "--old-jobs" in named("select", "Select test suites")["run"] and "attempts/" in named("select", "Select test suites")["run"]


def test_reword_reuse_is_off_until_enabled_and_compares_the_tree_this_run_checked_out():
    run = named("select", "Select test suites")["run"]
    assert 'if [ "${{ vars.CI_REWORD_REUSE }}" = 1 ] &&' in run
    # The merge checkout tree, not the pull request head's: a base that moved since the old run never reuses it.
    assert """--tree "$(git rev-parse 'HEAD^{tree}')\"""" in run and 'HEAD_SHA^{tree}' not in run


WINDOWS = {"pg-spawnd": "cargo test -p pg-spawnd", "pg-worker-core": "cargo test -p pg-worker-core"}


@pytest.mark.parametrize("package", sorted(WINDOWS))
def test_the_native_windows_blocking_run_is_the_shared_entry_point_for_its_own_package(tmp_path, package):
    """CI and the pre-push Windows check run the same `ci-flake cargo-test --package P`; the skips live in that binary."""
    step = named("rust-windows", WINDOWS[package])
    target = tmp_path / "target"
    (target / "debug").mkdir(parents=True)
    stub(target / "debug/ci-flake.exe", 'echo "$*" >> "$RUNNER_TEMP/calls"\n')
    done, _, _ = run_script(step["run"], tmp_path, {"CARGO_TARGET_DIR": str(target), "GITHUB_WORKSPACE": str(tmp_path)}, tmp_path)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "calls").read_text().strip() == f"cargo-test --package {package} --log {tmp_path}/cargo-win-{'spawnd' if package == 'pg-spawnd' else 'core'}.log"


def test_the_windows_job_pins_the_same_ledger_and_runs_the_reporting_step_without_blocking():
    assert named("rust-windows", "Quarantine ledger and run timestamp")["with"]["as-of"] == "${{ needs.select.outputs.as-of }}"
    report = named("rust-windows", "Quarantined Rust tests on Windows (reporting, never blocking)")
    assert report["continue-on-error"] is True and report["run"].count("--quarantine-only 1") == 2
    assert index("rust-windows", "Quarantine ledger and run timestamp") < index("rust-windows", "cargo test -p pg-spawnd")


def test_every_ledger_pin_reads_the_trusted_base_for_pull_requests_and_merge_groups():
    pins = [step for job in JOBS.values() for step in job["steps"] if step.get("uses") == "./.github/actions/quarantine-ledger"]
    assert len(pins) == 6   # pytest, pytest-postgres, rust, rust-windows, rust-worker, ci-gate
    for step in pins:
        base = step["with"]["base-sha"]
        assert "github.event.pull_request.base.sha" in base and "github.event.merge_group.base_sha" in base, step["name"]


def test_ci_pytest_steps_leave_the_classification_to_the_one_retry():
    assert named("pytest", "pytest")["env"]["CI_CLASSIFY_RETRY"] == "1"
    assert named("pytest-postgres", "pytest (contract suite + keeper storage tests on Postgres)")["env"]["CI_CLASSIFY_RETRY"] == "1"


@pytest.mark.parametrize("job,step", [("rust", "Retry the failed Rust tests once at the same SHA"),
                                       ("rust-worker", "Retry the failed worker tests once at the same SHA"),
                                       ("rust-windows", "Retry the failed pg-spawnd tests once at the same SHA"),
                                       ("rust-windows", "Retry the failed pg-worker-core tests once at the same SHA"),
                                       ("rust", "Retry the failed native writer tests once at the same SHA")])
def test_every_rust_runner_path_is_classified_by_the_shared_retry_action(job, step):
    retry = named(job, step)
    assert retry["uses"] == "./.github/actions/cargo-retry" and retry["continue-on-error"] is True
    assert "failure()" in retry["if"] and ".outcome == 'failure'" in retry["if"]
    assert retry["with"]["command"].startswith(("cargo test --no-fail-fast", "cargo run --manifest-path ../Cargo.toml"))


def test_the_worker_job_skips_only_its_own_package_entries_and_keeps_the_log(tmp_path):
    target = tmp_path / "target"
    (target / "debug").mkdir(parents=True)
    stub(target / "debug/ci-flake", "printf '%s\\n' 'pg-worker a::b' 'pg-spawnd c::d'\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir / "cargo", 'echo "cargo $*" >> "$RUNNER_TEMP/calls"\n')
    done, _, _ = run_script(named("rust-worker", "cargo test")["run"], tmp_path, {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "CARGO_TARGET_DIR": str(target),
        "PYTEST_QUARANTINE_FILE": "l", "PYTEST_QUARANTINE_AS_OF": "t"}, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "-- cargo test --no-fail-fast -- --exact --skip a::b" in (tmp_path / "calls").read_text()
    assert "c::d" not in (tmp_path / "calls").read_text() and (tmp_path / "cargo-worker-first.log").is_file()


def test_a_failing_retry_still_reaches_the_classification_in_both_retry_actions(tmp_path):
    """The action steps run under bash -e: the retry's own failure must not stop the classification."""
    work, bin_dir = retry_fixture(tmp_path, 2)
    stub(bin_dir / "python", 'echo "python $*" >> "$RUNNER_TEMP/calls"\nexit 1\n')
    done, _, _ = run_script(action_script("pytest-retry"), work, {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "SUITE": "sqlite", "FIRST_LOG": str(tmp_path / "first.log"), "RUN_ID": "9",
        "RUN_ATTEMPT": "1", "RUNNER_ID": "r", "HEAD_SHA": "b" * 40}, tmp_path)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0].startswith("python -m pytest") and calls[1].startswith("classify")
    assert "retry exit status: 1" in (tmp_path / "flake-sqlite/second.log").read_text()
    # the Rust action: the same shape with a cargo that fails
    rust = tmp_path / "rust"
    (rust / "rust").mkdir(parents=True)
    stub(bin_dir / "cargo", 'echo "cargo $*" >> "$RUNNER_TEMP/calls"\nexit 101\n')
    (tmp_path / "calls").unlink()
    script = yaml.safe_load((ROOT / ".github/actions/cargo-retry/action.yml").read_text())["runs"]["steps"][0]["run"]
    done, _, _ = run_script(script, rust, {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "SUITE": "cargo", "FIRST_LOG": str(tmp_path / "first.log"), "RETRY_COMMAND": "cargo test --no-fail-fast",
        "FLAKE_BIN": str(work / "rust/target/release/ci-flake"), "WORKDIR": "rust", "RUN_ID": "9", "RUN_ATTEMPT": "1", "RUNNER_ID": "r", "HEAD_SHA": "c" * 40}, tmp_path)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "cargo test --no-fail-fast -- --exact tests/t.py::t0 tests/t.py::t1" and calls[1].startswith("classify")
    assert "retry exit status: 101" in (tmp_path / "flake-cargo/second.log").read_text()


# --- the downloaded pg-spawnd is verified and staged for the fleet fixture ------------------------------------------

def fetch_fixture(tmp_path):
    repo = tmp_path / "repo"
    release = repo / "rust/target/release"
    release.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],  # noqa: E731
                                    check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q", "-b", "main")
    (repo / "f").write_text("x")
    run("add", "-A")
    run("commit", "-qm", "c")
    (release / "pg-spawnd").write_bytes(b"spawnd")
    (release / "ci-flake").write_bytes(b"flake")
    subprocess.run("sha256sum pg-spawnd ci-flake > pg-spawnd.sha256", shell=True, cwd=release, check=True)
    (release / "pg-spawnd.source").write_text(run("rev-parse", "HEAD") + "\n")
    return repo, release


def fetch_script():
    return yaml.safe_load((ROOT / ".github/actions/fetch-spawnd/action.yml").read_text())["runs"]["steps"][1]["run"]


def test_the_downloaded_binary_is_verified_and_staged_for_the_fleet_fixture(tmp_path):
    repo, release = fetch_fixture(tmp_path)
    env = {"GITHUB_WORKSPACE": str(repo)}
    done, env_file, _ = run_script(fetch_script(), repo, env, tmp_path)
    assert done.returncode == 0, done.stderr
    assert f"PLANGRAPH_TEST_FLEET_SPAWND={release / 'pg-spawnd'}" in env_file.read_text()
    assert os.access(release / "pg-spawnd", os.X_OK)
    (release / "pg-spawnd").write_bytes(b"tampered")
    tampered, _, _ = run_script(fetch_script(), repo, env, tmp_path)
    assert tampered.returncode != 0


def test_a_binary_built_from_another_commit_is_not_staged(tmp_path):
    repo, release = fetch_fixture(tmp_path)
    (release / "pg-spawnd.source").write_text("0" * 40 + "\n")
    done, env_file, _ = run_script(fetch_script(), repo, {"GITHUB_WORKSPACE": str(repo)}, tmp_path)
    assert done.returncode != 0 and "built from" in done.stderr and "PLANGRAPH_TEST_FLEET_SPAWND" not in env_file.read_text()


@pytest.mark.parametrize("job", ["pytest", "pytest-postgres"])
def test_the_real_fleet_fixture_runs_on_the_downloaded_binary_in_every_shard_under_the_pinned_ledger(job):
    names = [step.get("name") for step in steps(job)]
    smoke = "Exercise the fleet fixture on the downloaded pg-spawnd"
    assert names.index("Fetch the fleet projection helper built once for this run") < names.index("Install test deps") < names.index("Quarantine ledger and run timestamp")
    assert names.index("Quarantine ledger and run timestamp") < names.index(smoke) < names.index(
        "pytest" if job == "pytest" else "pytest (contract suite + keeper storage tests on Postgres)")
    step = named(job, smoke)
    # PYTEST_SHARD 0/1 keeps every test and lets a run that deselects its only test finish green (tests/conftest.py).
    assert step["env"]["PYTEST_SHARD"] == "0/1" and step["uses"] == "./.github/actions/bounded-step"
    assert "test_fleet_samples_and_missing_host" in step["with"]["command"]


def test_an_active_quarantine_entry_for_the_fleet_test_leaves_the_smoke_run_and_an_expired_one_does_not(tmp_path):
    test_id = "tests/test_fleet_snapshot.py::test_fleet_samples_and_missing_host"
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({test_id: {"owner": "plangraph:FIX", "starts": "2026-11-24T00:00:00+00:00", "expires": "2026-11-25T00:00:00+00:00"}}))
    command = named("pytest", "Exercise the fleet fixture on the downloaded pg-spawnd")["with"]["command"].split()

    def smoke(as_of):
        python, env = python_test_env()
        env.pop("PLANGRAPH_TEST_FLEET_SPAWND", None)
        env |= {"PYTEST_SHARD": "0/1", "PYTEST_QUARANTINE_FILE": str(ledger), "PYTEST_QUARANTINE_AS_OF": as_of}
        return subprocess.run([python, *command[1:]], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    active = smoke("2026-11-24T12:00:00+00:00")
    assert active.returncode == 0 and "60 deselected" in active.stdout, active.stdout[-300:]   # the -k filter's 59 plus the test itself
    expired = smoke("2026-11-25T00:00:00+00:00")   # the expiry instant: the test blocks again and runs (no binary is built here)
    assert "59 deselected" in expired.stdout and any(f"1 {word}" in expired.stdout for word in ("passed", "failed", "error"))


def test_the_build_job_ships_a_manifest_with_the_binaries():
    upload = next(step for step in steps("build-spawnd") if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert {"rust/target/release/pg-spawnd.sha256", "rust/target/release/pg-spawnd.source"} <= set(upload["with"]["path"].split())
    assert index("build-spawnd", "Record the build manifest") < steps("build-spawnd").index(upload)


def test_the_python_314_native_writer_run_has_the_quarantine_and_the_retry(tmp_path):
    target = tmp_path / "target"
    (target / "debug").mkdir(parents=True)
    stub(target / "debug/ci-flake", "printf '%s\\n' 'pg-spawnd native::case' 'pg-worker x::y'\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir / "cargo", 'echo "cargo $*" >> "$RUNNER_TEMP/calls"\n')
    done, _, _ = run_script(named("rust", "Native writer lifecycle (Python 3.14)")["run"], tmp_path, {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "CARGO_TARGET_DIR": str(target), "GITHUB_WORKSPACE": str(tmp_path),
        "PYTEST_QUARANTINE_FILE": "l", "PYTEST_QUARANTINE_AS_OF": "t"}, tmp_path)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "calls").read_text().strip() == "cargo test --no-fail-fast -p pg-spawnd --test native_writer -- --exact --skip native::case"
    assert index("rust", "Native writer lifecycle (Python 3.14)") < index("rust", "Retry the failed native writer tests once at the same SHA")
    # the Python 3.14 setup still comes first
    assert index("rust", "Native writer lifecycle (Python 3.14)") > max(i for i, step in enumerate(steps("rust")) if step.get("with", {}).get("python-version") == "3.14")
