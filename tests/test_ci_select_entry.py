"""Part entry: the entry set, the readiness verdict and the per-part cost record (LAND-PART-ENTRY-CHECKS)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("ci_select_entry", TOOLS / "ci_select.py")
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def tree(tmp_path, files, base_files):
    """A repository with base_files committed, then files committed on top: returns (root, base, head)."""
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name, text in base_files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "part")
    return root, base, git(root, "rev-parse", "HEAD")


BASE = {"src/plangraph/planner/provider_stop.py": "X = 1\n", "src/plangraph/__init__.py": "",
        "tests/test_quota.py": "from plangraph.planner import provider_stop\n\n\ndef test_x():\n    assert provider_stop.X\n",
        "rust/pg-spawnd/Cargo.toml": '[package]\nname = "pg-spawnd"\n', "rust/pg-spawnd/src/lib.rs": ""}


def test_a_python_only_quota_change_selects_rust_and_the_windows_job(tmp_path):
    root, base, head = tree(tmp_path, {"src/plangraph/planner/provider_stop.py": "X = 2\n"}, BASE)
    plan = ci_select.entry_set(root, base, head)
    assert plan["suites"]["rust"] and plan["windows"] is True
    assert "tests/test_quota.py" in plan["python_tests"]
    assert plan["fallback"] is False


def test_a_rust_change_selects_its_crates_and_the_windows_job(tmp_path):
    root, base, head = tree(tmp_path, {"rust/pg-spawnd/src/lib.rs": "// x\n"}, BASE)
    plan = ci_select.entry_set(root, base, head)
    assert plan["windows"] is True and plan["rust_packages"] == ["pg-spawnd"]


def test_a_docs_only_change_selects_nothing_and_no_windows_job(tmp_path):
    root, base, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    plan = ci_select.entry_set(root, base, head)
    assert plan["windows"] is False and plan["rust_packages"] == [] and plan["python_tests"] == []


def test_an_unknown_input_falls_back_to_the_full_selection(tmp_path):
    root, base, head = tree(tmp_path, {"mystery/thing.bin": "x"}, BASE)
    plan = ci_select.entry_set(root, base, head)
    assert plan["fallback"] is True and plan["suites"]["pytest"] is True


def test_the_entry_commands_run_every_selected_part(tmp_path):
    plan = {"python_tests": ["tests/test_a.py"], "rust_packages": ["pg-spawnd"], "fallback": False}
    commands = ci_select.entry_commands(plan)
    assert commands[0][1].endswith("affected_tests.py") and "--run" in commands[0] and "--local" in commands[0]
    assert ["cargo", "test", "--no-fail-fast", "-p", "pg-spawnd"] in commands


@pytest.mark.parametrize("attempts,flakes,problems", [
    ([], set(), ["no completed attempt on this head"]),
    ([(1, "success")], set(), []),
    ([(1, "failure")], set(), ["latest completed attempt 1 is failure"]),
    ([(1, "failure"), (2, "success")], set(), ["attempt 1 was failure and attempt 2 green: no flake disposition recorded"]),
    ([(1, "failure"), (2, "success")], {1}, []),
    ([(1, "success"), (2, "failure")], {1}, ["latest completed attempt 2 is failure"]),
    ([(1, "failure"), (2, None)], set(), ["latest completed attempt 1 is failure"]),   # attempt 2 still running
    ([(1, "cancelled")], set(), ["latest completed attempt 1 is cancelled"]),
])
def test_the_latest_completed_attempt_decides_and_a_red_then_green_needs_a_flake_disposition(attempts, flakes, problems):
    rows = [{"attempt": n, "conclusion": c} for n, c in attempts]
    assert ci_select.attempt_problems(rows, flakes) == problems


def test_ready_needs_fast_checks_entry_set_windows_and_a_clean_attempt():
    ok = {"fast_checks": True, "entry_set": True, "windows_required": False, "windows": None,
          "attempts": [{"attempt": 1, "conclusion": "success"}], "flake_dispositions": []}
    assert ci_select.ready_problems(ok) == []
    assert ci_select.ready_problems({**ok, "fast_checks": False}) == ["seconds-fast checks did not pass on this head"]
    assert ci_select.ready_problems({**ok, "entry_set": False}) == ["entry set did not pass on this head"]
    assert ci_select.ready_problems({**ok, "windows_required": True}) == ["native Windows entry run missing"]
    assert ci_select.ready_problems({**ok, "windows_required": True, "windows": False}) == ["native Windows entry run failed"]
    assert ci_select.ready_problems({**ok, "windows_required": True, "windows": True}) == []


def test_the_part_cost_record_splits_queue_time_from_run_time_and_cold_from_warm():
    jobs = [
        {"name": "pytest", "created_at": "2026-11-21T10:00:00Z", "started_at": "2026-11-21T10:02:00Z",
         "completed_at": "2026-11-21T10:12:00Z", "labels": ["ubuntu-24.04"], "runner_group_name": "GitHub Actions"},
        {"name": "rust", "created_at": "2026-11-21T10:00:00Z", "started_at": "2026-11-21T10:00:30Z",
         "completed_at": "2026-11-21T10:05:30Z", "labels": ["self-hosted", "linux"], "runner_group_name": "Default"},
    ]
    record = ci_select.part_cost(jobs, pr=1088, head="a" * 40)
    assert record["queue_seconds"] == 150 and record["run_seconds"] == 900
    assert record["jobs"][0] == {"name": "pytest", "queue_seconds": 120, "run_seconds": 600, "runner": "cold"}
    assert record["jobs"][1] == {"name": "rust", "queue_seconds": 30, "run_seconds": 300, "runner": "warm"}
    assert record["pr"] == 1088 and record["head"] == "a" * 40
    assert record["wall_seconds"] == 720


def test_the_cost_record_is_appended_to_telemetry(tmp_path):
    path = tmp_path / "telemetry" / "part-cost.jsonl"
    ci_select.write_part_cost(path, {"pr": 1, "head": "b" * 40})
    ci_select.write_part_cost(path, {"pr": 2, "head": "c" * 40})
    assert [json.loads(line)["pr"] for line in path.read_text().splitlines()] == [1, 2]


def test_a_part_whose_head_breaks_a_quota_test_fails_the_entry_run(tmp_path, capfd, monkeypatch):
    """The entry set runs on the part's own head: a red test on it refuses entry, a green one lets it in."""
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    broken = {"src/plangraph/planner/provider_stop.py": "X = 0\n"}
    root, base, head = tree(tmp_path, broken, BASE)
    args = ["entry-set", "--root", str(root), "--base", base, "--head", head, "--run"]
    assert ci_select.main(args) == 1
    result = capfd.readouterr()
    assert "assert 0" in result.out
    assert "entry-set: FAIL" in result.err


def test_a_part_whose_head_is_green_passes_the_entry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"src/plangraph/planner/provider_stop.py": "X = 5\n"}, BASE)
    assert ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run"]) == 0


def test_a_standalone_manifest_test_command_survives_into_the_entry_commands(tmp_path):
    """rust/worker is addressed by --manifest-path with --lib --bins: no crate name, still a command."""
    base = {**BASE, "rust/worker/Cargo.toml": '[package]\nname = "pg-worker"\n[workspace]\n',
            "rust/worker/src/lib.rs": "", "rust/Cargo.toml": '[workspace]\nmembers = ["pg-spawnd"]\nexclude = ["worker"]\n'}
    root, b, head = tree(tmp_path, {"rust/worker/src/lib.rs": "// x\n"}, base)
    plan = ci_select.entry_set(root, b, head)
    commands = ci_select.entry_commands(plan, b, head, root)
    worker = [c for c in commands if c[:3] == ["cargo", "test", "--no-fail-fast"] and "--manifest-path" in c]
    assert worker and worker[0][-2:] == ["--lib", "--bins"], commands


def test_the_entry_run_executes_workspace_and_standalone_nextest_commands(tmp_path, monkeypatch, capfd):
    base_files = {
        **BASE,
        "rust/Cargo.toml": '[workspace]\nmembers = ["pg-spawnd"]\nexclude = ["worker"]\n',
        "rust/.config/nextest.toml": "",
        "rust/worker/Cargo.toml": '[package]\nname = "pg-worker"\n[workspace]\n',
        "rust/worker/src/lib.rs": "",
    }
    root, base, head = tree(tmp_path, {
        "rust/pg-spawnd/src/lib.rs": "// workspace change\n",
        "rust/worker/src/lib.rs": "// standalone change\n",
    }, base_files)
    run = subprocess.run
    cargo_calls = []

    def fake_cargo(command, *args, **kwargs):
        if command[0] != "cargo":
            return run(command, *args, **kwargs)
        assert kwargs["cwd"] == root / "rust"
        cargo_calls.append(command)
        return subprocess.CompletedProcess(command, int("--manifest-path" in command))

    monkeypatch.setattr(subprocess, "run", fake_cargo)
    assert ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run"]) == 1
    result = capfd.readouterr()
    assert json.loads(result.out)["rust_packages"] == ["pg-spawnd"]
    assert "entry-set: FAIL: cargo nextest run" in result.err
    runner = ["cargo", "nextest", "run", "--config-file", str(root / "rust/.config/nextest.toml"),
              "--no-fail-fast", "--color", "never"]
    assert cargo_calls == [
        ["cargo", "nextest", "--version"],
        [*runner, "-p", "pg-spawnd"],
        [*runner, "--manifest-path", str(root / "rust/worker/Cargo.toml"), "--lib", "--bins"],
    ]
