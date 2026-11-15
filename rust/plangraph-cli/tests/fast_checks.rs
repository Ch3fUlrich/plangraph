use std::process::Command;

#[test]
fn fast_checks_matches_ci_for_three_fixture_diffs() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for path in [
        "README.md",
        "rust/worker/src/lib.rs",
        "rust/pg-spawnd/src/lib.rs",
    ] {
        let expected = Command::new("python3")
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
