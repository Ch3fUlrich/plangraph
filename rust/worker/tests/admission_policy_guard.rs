//! Compile production targets without dev-dependency feature unification.
#[test]
fn unrestricted_policy_is_test_only() {
    // A separate target directory avoids the outer cargo test's build lock.
    // Rustc checks callers under their actual cfg attributes, independently
    // of checkout paths, comments, and unrelated test modules.
    let target = tempfile::tempdir().unwrap();
    let output = std::process::Command::new(env!("CARGO"))
        .args(["check", "--offline", "--lib", "--bins"])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .env("CARGO_TARGET_DIR", target.path())
        .output()
        .expect("run production cargo check");
    assert!(
        output.status.success(),
        "production targets must compile without the test-only admission bypass:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
