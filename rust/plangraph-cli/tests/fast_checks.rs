#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::{fs, path::Path, process::Command};

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(root)
        .args(args)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.test")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.test")
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    String::from_utf8(output.stdout).unwrap().trim().into()
}

#[test]
fn fast_checks_matches_ci_for_three_fixture_diffs() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for path in [
        "README.md",
        "rust/worker/src/lib.rs",
        "rust/pg-spawnd/src/lib.rs",
    ] {
        let expected = test_python::command()
            .current_dir(&root)
            .args(["tools/ci_select.py", "select", "--files", path])
            .output()
            .unwrap();
        assert!(expected.status.success());
        let actual = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(&root)
            .args(["fast-checks", "--plan", "--files", path])
            .output()
            .unwrap();
        assert!(
            actual.status.success(),
            "{}",
            String::from_utf8_lossy(&actual.stderr)
        );
        let expected = String::from_utf8(expected.stdout).unwrap();
        let actual = String::from_utf8(actual.stdout).unwrap();
        for line in expected.lines() {
            assert!(actual.lines().any(|row| row == line), "{line}: {actual}");
        }
        if path.contains("worker/") || path.contains("pg-spawnd/") {
            assert!(actual.contains("rust/worker/Cargo.toml"));
        }
        if path == "README.md" {
            assert!(!actual.contains("cargo clippy"));
        }
    }
}

#[cfg(unix)]
#[test]
fn fast_checks_includes_reverse_dependents_and_rust_test_preflight() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let bin = tempfile::tempdir().unwrap();
    let path = std::env::join_paths(
        std::iter::once(bin.path().to_owned())
            .chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
    )
    .unwrap();
    for (version_status, runner) in [
        (
            0,
            "cargo nextest run --config-file rust/.config/nextest.toml",
        ),
        (101, "cargo test"),
    ] {
        plangraph_cli::atomic::write_executable(
            &bin.path().join("cargo"),
            format!(
                "#!/bin/sh\n[ \"$*\" = 'nextest --version' ] || exit 1\nexit {version_status}\n"
            )
            .as_bytes(),
        )
        .unwrap();
        let result = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(&root)
            .env("PATH", &path)
            .args([
                "fast-checks",
                "--plan",
                "--files",
                "rust/worker-core/src/lib.rs",
            ])
            .output()
            .unwrap();
        assert!(result.status.success());
        let plan = String::from_utf8(result.stdout).unwrap();
        let diagnostics = String::from_utf8(result.stderr).unwrap();
        assert_eq!(
            diagnostics.contains("falling back to cargo test"),
            version_status != 0,
        );
        for required in [
            "rust-test-env: python3 -m pytest -q tests/test_spawner_import_guard.py",
            "pg-spawnd/clippy:",
            "pg-worker/clippy:",
            "pg-worker/test: cargo run",
            &format!("--bin ci-temporal -- {runner}"),
            "--local --files-only --run",
        ] {
            assert!(plan.contains(required), "missing {required}: {plan}");
        }
        assert!(!plan.contains("temporal-spike/test:"));
    }
}

#[test]
fn fast_checks_keeps_external_rust_inputs_in_a_mixed_diff() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(root)
        .args([
            "fast-checks",
            "--plan",
            "--files",
            "docs/rust-hooks.md",
            "rust/plangraph-cli/src/main.rs",
        ])
        .output()
        .unwrap();
    assert!(output.status.success());
    let plan = String::from_utf8(output.stdout).unwrap();
    assert!(plan.contains("pg-hook/test:"));
    assert!(plan.contains("plangraph-cli/test:"));
    assert!(!plan.contains("pg-worker/test:"));
}

#[test]
fn fast_checks_scrubs_affected_tests_and_refuses_a_passing_base_replay() {
    let dir = tempfile::tempdir().unwrap();
    let cargo_cache = tempfile::tempdir().unwrap();
    let root = dir.path();
    git(root, &["init", "-q"]);
    let tools = root.join("tools");
    fs::create_dir_all(tools.join("split")).unwrap();
    for path in [
        "split/scrub_check.py",
        "check_file_size.py",
        "check_new_code_lang.py",
        "check_changelog_docs.py",
        "ci_commit_gate.py",
    ] {
        fs::write(tools.join(path), "pass\n").unwrap();
    }
    fs::write(tools.join("ci_select.py"), "print('pytest=true')\n").unwrap();
    fs::write(tools.join("affected_tests.py"),
        "import os\nassert 'AUTOOS_CTX_HARD' not in os.environ\nassert 'CLAUDE_JOB_DIR' not in os.environ\nassert 'CLASS_SEARCH_BASE' not in os.environ\nprint('affected environment verified')\n").unwrap();
    fs::write(root.join("test_example.py"), "result = 'pass'\n").unwrap();
    fs::write(
        root.join("pytest.py"),
        concat!(
            "import os, sys, test_example\n",
            "assert 'AUTOOS_CTX_HARD' not in os.environ\n",
            "assert 'CLAUDE_JOB_DIR' not in os.environ\n",
            "assert sys.argv[-2:] == ['--', 'test_example.py::test_example']\n",
            "if test_example.result == 'fail':\n",
            "    print('FAILED test_example.py::test_example - fixture failure')\n",
            "    sys.exit(1)\n",
            "print('1 passed')\n",
        ),
    )
    .unwrap();
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "base"]);
    let base = git(root, &["rev-parse", "HEAD"]);
    let run = |base: &str| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(root)
            .args(["fast-checks", "--base", base])
            .env(
                "PLANGRAPH_CARGO_TARGET_DIR",
                cargo_cache.path().join("target"),
            )
            .env("AUTOOS_CTX_HARD", "1")
            .env("CLAUDE_JOB_DIR", "fixture")
            .env("CLASS_SEARCH_BASE", "outer checkout base")
            .output()
            .unwrap()
    };

    fs::write(root.join("changed.txt"), "changed\n").unwrap();
    let output = run(&base);
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains("affected environment verified"));

    git(root, &["add", "."]);
    let note = format!("Fails-at-Base: {base} test_example.py::test_example");
    git(
        root,
        &["commit", "-qm", "claimed base failure", "-m", &note],
    );
    let output = run(&base);
    assert!(!output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("passed at base"));
    assert!(String::from_utf8_lossy(&output.stdout).contains("1 passed"));

    fs::write(root.join("test_example.py"), "result = 'fail'\n").unwrap();
    git(root, &["add", "test_example.py"]);
    git(root, &["commit", "-qm", "base with failure"]);
    let failing_base = git(root, &["rev-parse", "HEAD"]);
    fs::write(root.join("test_example.py"), "result = 'pass'\n").unwrap();
    git(root, &["add", "test_example.py"]);
    let note = format!("Fails-at-Base: {failing_base} test_example.py::test_example");
    git(
        root,
        &["commit", "-qm", "verified base failure", "-m", &note],
    );
    let output = run(&failing_base);
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout)
        .contains("FAILED test_example.py::test_example - fixture failure"));

    let malformed = "Class-Search: example -> 1 hit, 1 changed";
    git(
        root,
        &[
            "commit",
            "--allow-empty",
            "-qm",
            "malformed audit",
            "-m",
            malformed,
        ],
    );
    let malformed_commit = git(root, &["rev-parse", "HEAD"]);
    fs::write(
        tools.join("check_new_code_lang.py"),
        concat!(
            "import os, subprocess, sys\n",
            "base = os.environ['CLASS_SEARCH_BASE']\n",
            "commit = subprocess.check_output(['git', 'log', '-1', '--format=%H%n%b', base + '..HEAD'], text=True)\n",
            "assert 'Class-Search:' in commit\n",
            "print(commit)\n",
            "sys.exit(1)\n",
        ),
    ).unwrap();
    let output = run(&failing_base);
    assert!(!output.status.success(), "{output:?}");
    let log = String::from_utf8_lossy(&output.stdout);
    assert!(
        log.contains(&malformed_commit) && log.contains(malformed),
        "{log}"
    );
}

#[test]
fn fast_checks_builds_spawnd_before_affected_tests() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(root)
        .args([
            "fast-checks",
            "--plan",
            "--files",
            "rust/pg-spawnd/src/lib.rs",
            "tests/test_fleet_snapshot.py",
        ])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let plan = String::from_utf8(output.stdout).unwrap();
    let build = plan
        .find("cargo build --locked --manifest-path rust/Cargo.toml -p pg-spawnd --bin pg-spawnd")
        .expect(&plan);
    assert!(build < plan.find("affected-tests:").expect(&plan), "{plan}");
    assert!(plan.contains("--fleet-spawnd-prebuilt"), "{plan}");
}

#[test]
fn owned_fast_checks_removes_checkout_and_keeps_exact_head_stamp() {
    let repo = tempfile::tempdir().unwrap();
    let root = repo.path();
    let cache = tempfile::tempdir().unwrap();
    git(root, &["init", "-q"]);
    let tools = root.join("tools");
    fs::create_dir_all(tools.join("split")).unwrap();
    for name in [
        "split/scrub_check.py",
        "check_file_size.py",
        "check_new_code_lang.py",
        "check_changelog_docs.py",
        "ci_commit_gate.py",
    ] {
        fs::write(tools.join(name), "pass\n").unwrap();
    }
    fs::write(tools.join("ci_select.py"), "print('pytest=true')\n").unwrap();
    fs::write(tools.join("affected_tests.py"),
        "import os, pathlib, sys\nassert pathlib.Path.cwd() != pathlib.Path(os.environ['ORIGINAL_CHECKOUT'])\npathlib.Path('generated').write_text('test output')\nsys.exit(int(os.environ.get('FIXTURE_FAIL', '0')))\n").unwrap();
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "base"]);
    let base = git(root, &["rev-parse", "HEAD"]);
    let run = |fail: &str| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(root)
            .args(["fast-checks", "--owned-worktree", "--base", &base])
            .env("PLANGRAPH_CARGO_TARGET_DIR", cache.path().join("cargo"))
            .env("ORIGINAL_CHECKOUT", root)
            .env("FIXTURE_FAIL", fail)
            .output()
            .unwrap()
    };
    let before = git(root, &["worktree", "list", "--porcelain"]);
    for fail in ["0", "1"] {
        git(root, &["commit", "--allow-empty", "-qm", fail]);
        let commit = git(root, &["rev-parse", "HEAD"]);
        let output = run(fail);
        assert_eq!(output.status.success(), fail == "0", "{output:?}");
        assert_eq!(
            root.join(".git/plangraph-state/fast-checks")
                .join(&commit)
                .is_file(),
            fail == "0"
        );
        assert_eq!(
            git(root, &["worktree", "list", "--porcelain"]).replace(&commit, &base),
            before
        );
        assert!(!root.join("generated").exists());
    }
}

#[test]
fn fast_checks_plan_for_a_spawner_python_change_tests_pg_spawnd() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for path in [
        "src/plangraph/spawner/target.py",
        "src/plangraph/planner/legs.py",
    ] {
        let result = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(&root)
            .args(["fast-checks", "--plan", "--files", path])
            .output()
            .unwrap();
        assert!(result.status.success());
        let plan = String::from_utf8(result.stdout).unwrap();
        for required in ["pg-spawnd/clippy:", "pg-spawnd/test: cargo "] {
            assert!(plan.contains(required), "{path} misses {required}: {plan}");
        }
    }
}

fn registry(dir: &Path, mem_floor_mb: u64) -> std::path::PathBuf {
    let doc = serde_json::json!({"version": 1, "self": "worker-a", "hosts": [{
        "id": "worker-a", "class": "worker", "aliases": [],
        "transport": "keeper", "os": if cfg!(windows) { "windows" } else { "linux" },
        "state_dir": dir.to_str().unwrap(),
        "caps": {"max_runs": 4, "max_writers": 2, "max_seats": 2,
                 "max_sessions": 5, "mem_floor_mb": mem_floor_mb},
        "warmup": {"warmup_s": 1, "ramp_mb": {}},
        "session_mb": 1, "roles": [], "repos": {"plangraph": {
            "root": dir.to_str().unwrap(), "worktrees_root": dir.join("trees").to_str().unwrap(),
            "remote": "example", "import_roots": [], "cache_env": {}}}
    }]});
    let path = dir.join("hosts.json");
    fs::write(&path, doc.to_string()).unwrap();
    path
}

fn dispatch(dir: &Path, floor: u64, command: &[&str]) -> std::process::Output {
    let hosts = registry(dir, floor);
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["fast-checks", "--dispatch", "--registry"])
        .arg(&hosts)
        .args(["--repo", "plangraph", "--log"])
        .arg(dir.join("dispatch.log"))
        .arg("--")
        .args(command)
        .output()
        .unwrap()
}

#[test]
fn dispatch_report_counts_busy_attempts_apart_from_test_failures() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("dispatch.log");
    fs::write(
        &log,
        concat!(
            "{\"host\":\"a\",\"attempt\":1,\"verdict\":\"host_busy\"}\n",
            "{\"host\":\"b\",\"attempt\":2,\"verdict\":\"pass\"}\n",
            "{\"host\":\"a\",\"attempt\":1,\"verdict\":\"test_failure\"}\n",
        ),
    )
    .unwrap();
    let report = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["fast-checks", "--dispatch", "--report"])
        .arg(&log)
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8(report.stdout).unwrap().trim(),
        r#"{"passed":1,"host_busy":1,"test_failure":1,"ignored":0}"#
    );
}

#[test]
fn dispatch_refuses_a_host_below_its_memory_floor() {
    let dir = tempfile::tempdir().unwrap();
    let result = dispatch(dir.path(), u64::MAX, &["true"]);
    assert!(!result.status.success());
    let stderr = String::from_utf8_lossy(&result.stderr);
    assert!(stderr.contains("worker-a: MemAvailable"), "{stderr}");
    assert!(!dir.path().join("dispatch.log").exists());
}

#[cfg(target_os = "linux")]
#[test]
fn preflight_reports_a_hanging_rust_test_and_reaps_its_child() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    if !Command::new("cargo")
        .current_dir(&root)
        .args(["nextest", "--version"])
        .output()
        .is_ok_and(|probe| probe.status.success())
    {
        eprintln!("skipping hang test: cargo nextest --version failed");
        return;
    }

    let fixture = tempfile::tempdir().unwrap();
    let path = fixture.path();
    std::fs::create_dir(path.join("src")).unwrap();
    std::fs::write(
        path.join("Cargo.toml"),
        "[package]\nname = \"pg-timeout-fixture\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
    )
    .unwrap();
    std::fs::write(
        path.join("src/lib.rs"),
        r#"
#[test]
fn hangs_with_child() {
    let child = std::process::Command::new("sleep").arg("120").spawn().unwrap();
    std::fs::write(std::env::var_os("CHILD_PID").unwrap(), child.id().to_string()).unwrap();
    loop { std::thread::park(); }
}
"#,
    )
    .unwrap();
    let config = std::fs::read_to_string(root.join("rust/.config/nextest.toml")).unwrap();
    let deadline = path.join("nextest.toml");
    assert_eq!(
        config.matches("period = \"120s\"").count(),
        1,
        "rust/.config/nextest.toml must keep exactly one slow-timeout period of 120s for this fixture to shorten"
    );
    std::fs::write(
        &deadline,
        config.replace("period = \"120s\"", "period = \"2s\""),
    )
    .unwrap();
    let pidfile = path.join("child.pid");
    let output = preflight_command()
        .current_dir(&root)
        .env("CHILD_PID", &pidfile)
        .args(["cargo", "nextest", "run", "--manifest-path"])
        .arg(path.join("Cargo.toml"))
        .arg("--config-file")
        .arg(deadline)
        .output()
        .unwrap();
    let log = String::from_utf8_lossy(&output.stdout);
    assert!(!output.status.success(), "{log}");
    assert!(
        log.contains("HUNG pg-timeout-fixture hangs_with_child"),
        "{log}\n{:?}",
        output.stderr
    );
    assert_child_stopped(&pidfile);
}

#[cfg(target_os = "linux")]
fn assert_child_stopped(pidfile: &std::path::Path) {
    let pid: u32 = std::fs::read_to_string(pidfile).unwrap().parse().unwrap();
    if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        assert!(
            stat.rsplit_once(") ").unwrap().1.starts_with("Z "),
            "{stat}"
        );
    }
}

#[cfg(target_os = "linux")]
fn preflight_command() -> Command {
    let mut command = test_python::command();
    command.args([
        "-c",
        r#"
import os, sys
from pathlib import Path
sys.path.insert(0, 'tools')
import preflight
preflight.check_timeout = lambda *args: int(os.environ.get('PREFLIGHT_FIXTURE_TIMEOUT', '45'))
code, output = preflight.run(sys.argv[1:], Path.cwd(), os.environ)
print(output, end='')
sys.exit(code)
"#,
    ]);
    command
}

#[cfg(target_os = "linux")]
#[test]
fn preflight_reports_a_nextest_timeout_without_an_installed_runner() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let fixture = tempfile::tempdir().unwrap();
    plangraph_cli::atomic::write_executable(
        &fixture.path().join("cargo"),
        br#"#!/bin/sh
[ "$1 $2" = 'nextest run' ] || exit 2
case "$3" in
    report-timeout)
        printf '    TIMEOUT [   2.004s] ( 1/ 1) pg-timeout-fixture hangs_with_child\n' >&2
        exit 100
        ;;
    stall)
        sleep 120 &
        printf '%s' "$!" > "$CHILD_PID"
        wait
        ;;
    *) exit 2 ;;
esac
"#,
    )
    .unwrap();
    let search_path = std::env::join_paths(
        std::iter::once(fixture.path().to_owned())
            .chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
    )
    .unwrap();
    let pidfile = fixture.path().join("child.pid");
    let run = |mode| {
        preflight_command()
            .current_dir(&root)
            .env("PATH", &search_path)
            .env("CHILD_PID", &pidfile)
            .env("PREFLIGHT_FIXTURE_TIMEOUT", "3")
            .args(["cargo", "nextest", "run", mode])
            .output()
            .unwrap()
    };

    let output = run("report-timeout");
    assert_eq!(output.status.code(), Some(100), "{output:?}");
    let log = String::from_utf8_lossy(&output.stdout);
    assert!(
        log.contains("HUNG pg-timeout-fixture hangs_with_child"),
        "{log}"
    );

    let output = run("stall");
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let log = String::from_utf8_lossy(&output.stdout);
    assert!(log.contains("check exceeded 3 second timeout"), "{log}");
    assert_child_stopped(&pidfile);
}

#[test]
fn fast_checks_plan_for_the_nextest_config_tests_the_replay_spike() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(&root)
        .args([
            "fast-checks",
            "--plan",
            "--files",
            "rust/.config/nextest.toml",
        ])
        .output()
        .unwrap();
    assert!(result.status.success());
    let plan = String::from_utf8(result.stdout).unwrap();
    assert!(plan.contains("rust-spike=true"), "{plan}");
    assert!(plan.contains("temporal-spike/test: cargo "), "{plan}");
}
