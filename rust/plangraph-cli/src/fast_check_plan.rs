//! Project CI's selector onto the workspace-specific local runners.
use super::{output, step, Result, Step};
use crate::fast_check_packages::packages;
use std::path::Path;

pub(super) fn cargo_step(manifest: &str, package: &str, verb: &str) -> Step {
    let mut args = Vec::new();
    if verb == "test" && manifest.starts_with("rust/worker/") {
        args.extend([
            "cargo",
            "run",
            "--manifest-path",
            "rust/Cargo.toml",
            "-p",
            "pg-spawnd",
            "--bin",
            "ci-temporal",
            "--",
        ]);
    }
    args.extend(["cargo", verb, "--manifest-path", manifest, "-p", package]);
    step(&format!("{package}/{verb}"), &args)
}

pub(super) fn plan(root: &Path, base: &str, paths: &[String]) -> Result<(String, Vec<Step>)> {
    let mut selector = vec![
        "python3".into(),
        "tools/ci_select.py".into(),
        "select".into(),
        "--files".into(),
    ];
    selector.extend_from_slice(paths);
    let selection = output(root, &selector)?;
    let enabled = |job: &str| {
        selection
            .lines()
            .any(|line| line.trim() == format!("{job}=true"))
    };
    let mut steps = vec![
        step(
            "scrub",
            &[
                "python3",
                "tools/split/scrub_check.py",
                ".",
                "--rules",
                "tools/split/manifest.toml",
                "--rules",
                "tools/split/scrub_ci.toml",
            ],
        ),
        step("file-size", &["python3", "tools/check_file_size.py"]),
        step(
            "new-code-lang",
            &["python3", "tools/check_new_code_lang.py"],
        ),
        step(
            "changelog-docs",
            &["python3", "tools/check_changelog_docs.py"],
        ),
        step(
            "commit-gate",
            &[
                "python3",
                "tools/ci_commit_gate.py",
                "check",
                "--base",
                base,
                "--head",
                "HEAD",
            ],
        ),
        step("diff-check", &["git", "diff", "--check", base]),
    ];
    let rust = ["rust", "rust-worker", "rust-spike"]
        .iter()
        .any(|job| enabled(job));
    if rust {
        // Reuse the existing guard against pytest dependencies in Rust jobs.
        steps.insert(
            0,
            step(
                "rust-test-env",
                &[
                    "python3",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_spawner_import_guard.py",
                ],
            ),
        );
        let mut external_selector = selector[..4].to_vec();
        external_selector.extend(paths.iter().filter(|p| !p.starts_with("rust/")).cloned());
        let external_rust = external_selector.len() > 4
            && output(root, &external_selector)?
                .lines()
                .any(|line| line.trim() == "rust=true");
        let mut selected = packages(root, paths, enabled("full") || external_rust)?;
        if enabled("rust-worker") && !selected.iter().any(|(_, n)| n == "pg-worker") {
            selected.push(("rust/worker/Cargo.toml".into(), "pg-worker".into()));
        }
        if selected
            .iter()
            .any(|(m, _)| !m.starts_with("rust/worker/") && !m.starts_with("rust/spikes/"))
        {
            steps.push(step(
                "jail-helper",
                &[
                    "cargo",
                    "build",
                    "--manifest-path",
                    "rust/Cargo.toml",
                    "-p",
                    "pg-jail",
                ],
            ));
        }
        for (manifest, name) in selected {
            if manifest.starts_with("rust/worker/") && !enabled("rust-worker")
                || manifest.starts_with("rust/spikes/") && !enabled("rust-spike")
            {
                continue;
            }
            let workspace =
                if manifest.starts_with("rust/worker/") || manifest.starts_with("rust/spikes/") {
                    manifest
                } else {
                    "rust/Cargo.toml".into()
                };
            for (verb, tail) in [
                ("fmt", vec!["--", "--check"]),
                ("clippy", vec!["--all-targets", "--", "-D", "warnings"]),
                ("test", vec![]),
            ] {
                let mut planned = cargo_step(&workspace, &name, verb);
                planned.args.extend(tail.into_iter().map(String::from));
                steps.push(planned);
            }
        }
    }
    if enabled("pytest") || enabled("pytest-postgres") || enabled("browser") {
        let mut affected = step(
            "affected-tests",
            &[
                "python3",
                "tools/affected_tests.py",
                "--base",
                base,
                "--local",
                "--files-only",
                "--run",
            ],
        );
        for path in paths {
            affected.args.extend(["--changed".into(), path.clone()]);
        }
        if root.join("tests/test_fleet_snapshot.py").is_file() {
            let mut select = affected.args.clone();
            select.retain(|arg| arg != "--run");
            let selected = output(root, &select)?;
            let plan: serde_json::Value = serde_json::from_str(
                selected
                    .lines()
                    .next()
                    .ok_or("missing affected test plan")?,
            )
            .map_err(|error| error.to_string())?;
            if plan["tests"]
                .as_array()
                .ok_or("invalid affected test plan")?
                .iter()
                .any(|path| path == "tests/test_fleet_snapshot.py")
            {
                // Cargo verifies the sources even when another checkout built the cache last.
                steps.push(step(
                    "fleet-spawnd",
                    &[
                        "cargo",
                        "build",
                        "--locked",
                        "--manifest-path",
                        "rust/Cargo.toml",
                        "-p",
                        "pg-spawnd",
                        "--bin",
                        "pg-spawnd",
                    ],
                ));
                affected.args.push("--fleet-spawnd-prebuilt".into());
            }
        }
        steps.push(affected);
    }
    Ok((selection, steps))
}
