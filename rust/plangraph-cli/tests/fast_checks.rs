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

#[test]
fn fast_checks_includes_reverse_dependents_and_rust_test_preflight() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(root)
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
    for required in [
        "rust-test-env: python3 -m pytest -q tests/test_spawner_import_guard.py",
        "pg-spawnd/clippy:",
        "pg-worker/clippy:",
        "pg-worker/test: cargo run",
        "--bin ci-temporal -- cargo test",
        "--local --files-only --run",
    ] {
        assert!(plan.contains(required), "missing {required}: {plan}");
    }
    assert!(!plan.contains("temporal-spike/test:"));
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
    let verify = plan
        .find("fleet-spawnd-verify: python3 tests/_spawnd.py")
        .expect(&plan);
    assert!(
        build < verify && verify < plan.find("affected-tests:").expect(&plan),
        "{plan}"
    );
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
