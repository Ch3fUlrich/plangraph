"""Part entry: the entry set, the readiness verdict and the per-part cost record (LAND-PART-ENTRY-CHECKS)."""
import importlib.util
import json
import subprocess
import sys
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
    runner = ["cargo", "nextest", "run", "--config-file", str(root / "rust/.config/nextest.toml"),
              "--no-fail-fast", "--color", "never"]
    real_load = ci_select._load_tool
    preflight = real_load("preflight")
    monkeypatch.setattr(preflight, "rust_commands", lambda *_a, **_kw: [
        ["cargo", "fmt", "--check"],
        [*runner, "-p", "pg-spawnd"],
        [*runner, "--manifest-path", str(root / "rust/worker/Cargo.toml"), "--lib", "--bins"],
    ])
    monkeypatch.setattr(ci_select, "_load_tool", lambda name: preflight if name == "preflight" else real_load(name))
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
    assert cargo_calls == [
        [*runner, "-p", "pg-spawnd"],
        [*runner, "--manifest-path", str(root / "rust/worker/Cargo.toml"), "--lib", "--bins"],
    ]


def test_replay_matches_platform_and_changed_inventory(tmp_path, monkeypatch):
    manifest = "rust/pg-spawnd/Cargo.toml"
    old = "rust/pg-spawnd/tests/old.rs"
    new = "rust/pg-spawnd/tests/renamed.rs"
    original = """// A stable comment makes Git detect this as a rename.
#[cfg(windows)]
#[test]
fn native_only() {
    assert_eq!(1, 1);
}

#[test]
fn unchanged() {
    assert!(true);
}
"""
    workflow = "jobs:\n  rust-windows:\n    steps:\n      - run: cargo test -p pg-spawnd\n"
    root, base, _ = tree(tmp_path, {"docs/x.md": "x\n"},
                         {**BASE, old: original, ".github/workflows/tests.yml": workflow})
    git(root, "mv", old, new)
    (root / new).write_text(original.replace("assert_eq!(1, 1)", "assert_eq!(2, 2)"))
    git(root, "commit", "-qam", "edit a renamed Windows test")
    head = git(root, "rev-parse", "HEAD")
    plan = ci_select._load_tool("merge_batch_plan")
    inventory = plan.replay_inventory(root, base, head)
    assert inventory == {"windows": {"python": [], "rust": [[manifest, new, "native_only"]]}}

    telemetry = tmp_path / "telemetry"
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(telemetry))
    receipt_dir = telemetry / "entry-replay" / head
    receipt_dir.mkdir(parents=True)
    receipt = receipt_dir / "windows.json"
    valid = {"base": base, "head": head, "environment": "windows",
             "inventory": inventory["windows"], "runs": 5, "ok": True,
             "observations": {f"{new}::native_only": ["pass"] * 5}}
    receipt.write_text(json.dumps(valid))
    facts = {"fast_checks": True, "entry_set": True, "attempts": [{"attempt": 1, "conclusion": "success"}],
             **plan.replay_facts(root, base, head)}
    assert ci_select.ready_problems(facts) == []

    for key, value in [("environment", "default"), ("inventory", {"python": [], "rust": []}),
                       ("runs", 4), ("head", base), ("observations", {f"{new}::native_only": ["pass"] * 4})]:
        receipt.write_text(json.dumps({**valid, key: value}))
        problems = ci_select.ready_problems({**facts, **plan.replay_facts(root, base, head)})
        assert any("windows" in problem and key in problem for problem in problems), problems

    no_test_inventory = plan.replay_facts(root, base, git(root, "rev-parse", "HEAD^"))
    assert ci_select.ready_problems({**facts, **no_test_inventory}) == []
    assert ci_select.ready_problems({**facts, **no_test_inventory, "attempts": []}) == [
        "no completed attempt on this head"]


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Cargo")
def test_native_windows_replay_records_five_observations(tmp_path, monkeypatch):
    path = "rust/pg-spawnd/tests/replay_windows.rs"
    manifest = '[package]\nname = "pg-spawnd"\nversion = "0.1.0"\nedition = "2021"\n'
    workflow = "jobs:\n  rust-windows:\n    steps:\n      - run: cargo test -p pg-spawnd\n"
    root, base, head = tree(tmp_path, {
        path: '#![cfg(windows)]\n#[test]\nfn native_only() { assert!(cfg!(windows)); }\n',
    }, {"rust/pg-spawnd/Cargo.toml": manifest, ".github/workflows/tests.yml": workflow})
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "cargo"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    telemetry = tmp_path / "telemetry"
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(telemetry))
    args = ["entry-set", "--root", str(root), "--base", base, "--head", head, "--replay"]
    assert ci_select.main(args + ["--environment", "windows"]) == 0
    path_to_receipt = telemetry / "entry-replay" / head / "windows.json"
    receipt = json.loads(path_to_receipt.read_text(encoding="utf-8"))
    assert receipt["observations"] == {f"{path}::native_only": ["pass"] * 5}
    assert all((path_to_receipt.parent / attempt["stderr"]).is_file() for attempt in receipt["attempts"])


@pytest.mark.skipif(sys.platform == "win32", reason="default replay runs on Linux")
def test_inconsistent_replay_preserves_each_log(tmp_path, monkeypatch):
    test_path = "tests/test_flake.py"
    content = """import os
from pathlib import Path


def test_flake():
    print("stdout evidence")
    print("stderr evidence", file=__import__("sys").stderr)
    counter = Path(os.environ["REPLAY_FIXTURE_COUNT"])
    seen = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen + 1))
    assert seen != 2
"""
    browser = "tests/browser/test_browser.py"
    files = {test_path: content, browser: "import pytest\n@pytest.mark.browser\ndef test_browser():\n    pass\n"}
    root, base, head = tree(tmp_path, files, {"README.md": "fixture\n"})
    telemetry = tmp_path / "telemetry"
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(telemetry))
    monkeypatch.setenv("REPLAY_FIXTURE_COUNT", str(tmp_path / "counter"))
    args = ["entry-set", "--root", str(root), "--base", base, "--head", head, "--replay"]
    receipt = telemetry / "entry-replay" / head / "default.json"

    assert ci_select.main(args) == 1
    first = json.loads(receipt.read_text())
    assert first["flaky"] == [f"{test_path}::test_flake"]
    assert first["runs"] == 5 and not first["ok"]
    logs = [receipt.parent / attempt[stream] for attempt in first["attempts"] for stream in ("stdout", "stderr")]
    assert len(logs) == len(set(logs)) == 10
    assert all("stdout evidence" in item.read_text() for item in logs[::2])
    assert all("stderr evidence" in item.read_text() for item in logs[1::2])
    before = {item: item.read_bytes() for item in logs}

    assert ci_select.main(args) == 0
    second = json.loads(receipt.read_text())
    assert not second["flaky"] and second["ok"] and second["runs"] == 5
    assert {item: item.read_bytes() for item in logs} == before
    assert all(receipt.parent / row["stdout"] not in logs for row in second["attempts"])
    assert ci_select.main(args + ["--environment", "browser"]) == 0
    browser_run = json.loads(receipt.with_name("browser.json").read_text())
    assert browser_run["observations"] == {f"{browser}::test_browser": ["pass"] * 5}
    assert all(receipt.parent / row["stdout"] not in logs for row in browser_run["attempts"])
    assert json.loads(receipt.read_text()) == second
    assert json.loads((receipt.parent / first["record"]).read_text()) == first
