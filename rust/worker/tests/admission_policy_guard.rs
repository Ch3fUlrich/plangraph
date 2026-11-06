//! Compile production targets without dev-dependency feature unification.
use std::{path::Path, process::Command};

fn check(manifest: &Path, target: &Path, features: &[String]) -> std::process::Output {
    let mut command = Command::new(env!("CARGO"));
    command
        .args([
            "check",
            "--offline",
            "--workspace",
            "--lib",
            "--bins",
            "--no-default-features",
            "--manifest-path",
        ])
        .arg(manifest)
        .env("CARGO_TARGET_DIR", target);
    if !features.is_empty() {
        command.arg("--features").arg(features.join(","));
    }
    command.output().expect("run production cargo check")
}

#[test]
fn unrestricted_policy_is_test_only() {
    // A separate target directory avoids the outer cargo test's build lock.
    // Rustc checks callers under their actual cfg attributes, independently
    // of checkout paths, comments, and unrelated test modules.
    let target = tempfile::tempdir().unwrap();
    let worker = Path::new(env!("CARGO_MANIFEST_DIR"));
    for manifest in [
        worker.join("Cargo.toml"),
        worker.parent().unwrap().join("Cargo.toml"),
    ] {
        let metadata = Command::new(env!("CARGO"))
            .args([
                "metadata",
                "--offline",
                "--no-deps",
                "--format-version",
                "1",
                "--manifest-path",
            ])
            .arg(&manifest)
            .output()
            .expect("read workspace metadata");
        assert!(
            metadata.status.success(),
            "{}",
            String::from_utf8_lossy(&metadata.stderr)
        );
        let metadata: serde_json::Value = serde_json::from_slice(&metadata.stdout).unwrap();
        let members = metadata["workspace_members"].as_array().unwrap();
        let mut combinations = vec![Vec::<String>::new()];
        for package in metadata["packages"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|package| members.contains(&package["id"]))
        {
            let features = package["features"].as_object().unwrap();
            // Exclude aliases that transitively enable test-only features too.
            let mut forbidden = vec!["test".to_owned(), "test-support".to_owned()];
            loop {
                let additions: Vec<_> = features
                    .iter()
                    .filter(|(name, values)| {
                        !forbidden.contains(name)
                            && values.as_array().unwrap().iter().any(|value| {
                                let value = value.as_str().unwrap();
                                forbidden.iter().any(|blocked| {
                                    value == blocked || value.ends_with(&format!("/{blocked}"))
                                })
                            })
                    })
                    .map(|(name, _)| name.clone())
                    .collect();
                if additions.is_empty() {
                    break;
                }
                forbidden.extend(additions);
            }
            for feature in features.keys().filter(|name| !forbidden.contains(name)) {
                let feature = format!("{}/{}", package["name"].as_str().unwrap(), feature);
                let enabled: Vec<_> = combinations
                    .iter()
                    .map(|combination| {
                        let mut combination = combination.clone();
                        combination.push(feature.clone());
                        combination
                    })
                    .collect();
                combinations.extend(enabled);
            }
        }
        // Check every subset, not just all-features: cfg(not(feature = ...))
        // callers must also be rejected. Never enable test or test-support.
        for features in combinations {
            let output = check(&manifest, target.path(), &features);
            assert!(
                output.status.success(),
                "production targets in {} with {features:?} must compile without the test-only admission bypass:\n{}",
                manifest.display(),
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }

    // A successful production build alone cannot prove that the API is gated.
    // Compile a real downstream caller without dev-dependency unification.
    let probe = tempfile::tempdir().unwrap();
    std::fs::create_dir(probe.path().join("src")).unwrap();
    std::fs::write(probe.path().join("Cargo.toml"), format!(
        "[package]\nname = \"admission-policy-probe\"\nversion = \"0.0.0\"\nedition = \"2021\"\n[workspace]\n[dependencies]\npg-worker-core = {{ path = {:?} }}\n",
        worker.parent().unwrap().join("worker-core")
    )).unwrap();
    std::fs::write(
        probe.path().join("src/main.rs"),
        "fn main() { let _ = pg_worker_core::config::Config::allow_all_for_tests(); }\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO"))
        .args(["check", "--offline", "--manifest-path"])
        .arg(probe.path().join("Cargo.toml"))
        .env("CARGO_TARGET_DIR", target.path())
        .output()
        .expect("compile bypass probe");
    assert!(
        !output.status.success()
            && String::from_utf8_lossy(&output.stderr).contains("E0599")
            && String::from_utf8_lossy(&output.stderr).contains("allow_all_for_tests"),
        "the test-only admission bypass must be unavailable to production callers:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
