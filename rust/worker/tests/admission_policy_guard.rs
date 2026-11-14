//! Compile production targets without dev-dependency feature unification.
use std::{path::Path, process::Command};

fn check(
    manifest: &Path,
    target: &Path,
    defaults: bool,
    features: &[String],
) -> std::process::Output {
    let mut command = Command::new(env!("CARGO"));
    command
        .args(["check", "--workspace", "--lib", "--bins", "--manifest-path"])
        .arg(manifest)
        .env("CARGO_TARGET_DIR", target);
    if !defaults {
        command.arg("--no-default-features");
    }
    if !features.is_empty() {
        command.arg("--features").arg(features.join(","));
    }
    command.output().expect("run production cargo check")
}

#[test]
fn unrestricted_policy_is_test_only() {
    // Allow Cargo to fetch dependencies: CI may not have cached the parent
    // workspace's dev-dependencies. Resolution/build failures must still fail.
    // A separate target directory avoids the outer cargo test's build lock.
    // PG_ADMISSION_GUARD_TARGET_DIR names a directory that persists between
    // runs, so the nested checks rebuild only what changed; without it every
    // run compiles both workspaces from scratch in a temporary directory.
    // Rustc checks callers under their actual cfg attributes, independently
    // of checkout paths, comments, and unrelated test modules.
    let scratch = tempfile::tempdir().unwrap();
    let target = match std::env::var_os("PG_ADMISSION_GUARD_TARGET_DIR") {
        Some(dir) if !dir.is_empty() => std::path::PathBuf::from(dir),
        _ => scratch.path().to_path_buf(),
    };
    std::fs::create_dir_all(&target).unwrap();
    let target = target.as_path();
    let worker = Path::new(env!("CARGO_MANIFEST_DIR"));
    for manifest in [
        worker.join("Cargo.toml"),
        worker.parent().unwrap().join("Cargo.toml"),
    ] {
        let metadata = Command::new(env!("CARGO"))
            .args([
                "metadata",
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
        let mut all_features = Vec::new();
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
            // The reverse transitive closure above includes `default` when
            // any default-enabled feature or alias reaches a forbidden feature.
            assert!(
                !forbidden.iter().any(|name| name == "default"),
                "{} default feature closure enables a forbidden test feature or alias",
                package["name"]
            );
            for feature in features.keys().filter(|name| !forbidden.contains(name)) {
                all_features.push(format!("{}/{}", package["name"].as_str().unwrap(), feature));
            }
        }
        // Exactly three production checks per workspace. The no-default run
        // also rejects cfg(not(feature = X)) callers for default-enabled X.
        // Never enable test or test-support (including their aliases).
        for (defaults, features) in [
            (false, Vec::new()),
            (true, Vec::new()),
            (false, all_features),
        ] {
            let output = check(&manifest, target, defaults, &features);
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
        .args(["check", "--manifest-path"])
        .arg(probe.path().join("Cargo.toml"))
        .env("CARGO_TARGET_DIR", target)
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
