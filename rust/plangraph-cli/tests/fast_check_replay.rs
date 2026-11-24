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

fn fixture(selection: &str) -> tempfile::TempDir {
    let fixture = tempfile::tempdir().unwrap();
    let root = fixture.path();
    git(root, &["init", "-q"]);
    fs::create_dir_all(root.join("tools/split")).unwrap();
    for name in [
        "split/scrub_check.py",
        "check_file_size.py",
        "check_new_code_lang.py",
        "check_changelog_docs.py",
        "ci_commit_gate.py",
        "affected_tests.py",
    ] {
        fs::write(root.join("tools").join(name), "pass\n").unwrap();
    }
    fs::write(
        root.join("tools/ci_select.py"),
        format!("print({selection:?})\n"),
    )
    .unwrap();
    fixture
}

fn commit(root: &Path, message: &str) -> String {
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", message]);
    git(root, &["rev-parse", "HEAD"])
}

fn run(root: &Path, base: &str) -> (bool, String) {
    let mut command = Command::new(env!("CARGO_BIN_EXE_plangraph"));
    let scratch = tempfile::tempdir().unwrap();
    if !root.join("rust/Cargo.toml").exists() {
        // This fixture selects only pytest. No Cargo command uses this stub cache.
        command.env("PLANGRAPH_CARGO_TARGET_DIR", scratch.path().join("cache"));
    }
    let out = command
        .current_dir(root)
        .args(["fast-checks", "--owned-worktree", "--base", base])
        .output()
        .unwrap();
    (
        out.status.success(),
        format!(
            "{}\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        ),
    )
}

fn stamp(root: &Path, head: &str) -> std::path::PathBuf {
    root.join(".git/plangraph-state/fast-checks").join(head)
}

#[test]
fn fast_checks_replays_changed_tests_without_trailers() {
    let fixture = fixture("pytest=true");
    let root = fixture.path();
    let test = root.join("tests/test_replay.py");
    fs::create_dir_all(test.parent().unwrap()).unwrap();
    fs::write(root.join("behavior.py"), "answer = 1\n").unwrap();
    fs::write(
        &test,
        "from behavior import answer\n\ndef test_changed():\n    assert answer >= 1\n",
    )
    .unwrap();
    let base = commit(root, "base");
    fs::write(&test, "from behavior import answer\n\ndef test_changed():\n    assert answer == 2\n\ndef test_new():\n    assert answer > 0\n").unwrap();
    fs::write(root.join("behavior.py"), "answer = 2\n").unwrap();
    let head = commit(root, "base-passing new test");
    let (ok, log) = run(root, &base);
    assert!(
        !ok && log.contains("tests/test_replay.py::test_new") && log.contains("passed at base"),
        "{log}"
    );
    assert!(!stamp(root, &head).exists());

    let exception = "Test-Replay-Exception: \"tests/test_replay.py::test_new\" Pure refactor guard";
    git(
        root,
        &[
            "commit",
            "--allow-empty",
            "-qm",
            "Explain base pass",
            "-m",
            exception,
        ],
    );
    let (ok, log) = run(root, &base);
    assert!(ok && log.contains("Pure refactor guard"), "{log}");

    let changed = fs::read_to_string(&test)
        .unwrap()
        .replace("answer > 0", "answer >= 1");
    fs::write(&test, changed).unwrap();
    let head = commit(root, "Change the exempted test revision");
    let (ok, log) = run(root, &base);
    assert!(!ok && log.contains("test_new passed at base"), "{log}");
    assert!(!stamp(root, &head).exists());

    fs::write(&test, "from behavior import answer\n\ndef test_changed():\n    assert answer == 2\n\ndef test_new():\n    assert answer == 2\n").unwrap();
    let head = commit(root, "both tests detect the change");
    let (ok, log) = run(root, &base);
    assert!(ok && stamp(root, &head).is_file(), "{log}");
    assert!(
        log.contains("tests/test_replay.py::test_changed")
            && log.contains("tests/test_replay.py::test_new"),
        "{log}"
    );
    assert!(
        log.contains("base=failed") && log.contains("head=passed"),
        "{log}"
    );

    fs::write(
        &test,
        concat!(
            "import pytest\nfrom behavior import answer\n\n",
            "@pytest.mark.parametrize('expected', [2, 1], ids=['detects change', 'base pass'])\n",
            "def test_cases(expected):\n    assert answer >= expected\n",
        ),
    )
    .unwrap();
    let head = commit(root, "parametrized test with one base pass");
    let (ok, log) = run(root, &base);
    assert!(
        !ok && log.contains("test_cases[base pass] passed at base"),
        "{log}"
    );
    assert!(!stamp(root, &head).exists());

    fs::write(
        root.join("tests/conftest.py"),
        "import pytest\n\n@pytest.fixture\ndef new_fixture():\n    return 2\n",
    )
    .unwrap();
    fs::write(
        &test,
        "def test_missing_fixture(new_fixture):\n    assert new_fixture == 2\n",
    )
    .unwrap();
    let head = commit(root, "new fixture absent on base");
    let (ok, log) = run(root, &base);
    assert!(
        !ok && log.contains("test_missing_fixture") && log.contains("inconclusive"),
        "{log}"
    );
    assert!(!stamp(root, &head).exists());
}

#[test]
fn fast_checks_replays_colocated_rust_test_against_base_code() {
    let fixture = fixture("rust=true");
    let root = fixture.path();
    let file = root.join("rust/pg-jail/src/lib.rs");
    fs::create_dir_all(file.parent().unwrap()).unwrap();
    fs::create_dir_all(root.join("tests")).unwrap();
    fs::write(
        root.join("tests/test_spawner_import_guard.py"),
        "def test_guard():\n    pass\n",
    )
    .unwrap();
    fs::write(
        root.join("rust/Cargo.toml"),
        "[workspace]\nmembers = ['pg-jail']\nresolver = '2'\n",
    )
    .unwrap();
    for package in ["pg-jail", "worker", "spikes/temporal-spike"] {
        let path = root.join("rust").join(package);
        fs::create_dir_all(&path).unwrap();
        fs::write(
            path.join("Cargo.toml"),
            format!(
                "[package]\nname = '{}'\nversion = '0.0.0'\nedition = '2021'\n",
                package.rsplit('/').next().unwrap()
            ),
        )
        .unwrap();
    }
    fs::write(&file, "pub fn answer() -> u32 {\n    1\n}\n").unwrap();
    let base = commit(root, "old Rust implementation");
    fs::write(&file, "pub fn answer() -> u32 {\n    2\n}\n\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn detects_change() {\n        assert_eq!(super::answer(), 2);\n    }\n}\n").unwrap();
    let head = commit(root, "new Rust implementation and regression test");
    let (ok, log) = run(root, &base);
    assert!(ok && stamp(root, &head).is_file(), "{log}");
    assert!(
        log.contains("pg-jail:lib::tests::detects_change")
            && log.contains("base=failed")
            && log.contains("head=passed"),
        "{log}"
    );
}
