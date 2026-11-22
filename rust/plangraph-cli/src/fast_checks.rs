//! Bounded local projection of CI selection; the selector remains authoritative.
use crate::fast_check_packages::packages;
#[path = "fast_check_replay.rs"]
mod replay;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

pub(super) type Result<T> = std::result::Result<T, String>;

#[derive(Debug)]
struct Step {
    label: String,
    args: Vec<String>,
}
fn step(label: &str, args: &[&str]) -> Step {
    Step {
        label: label.into(),
        args: args.iter().map(|s| (*s).into()).collect(),
    }
}
fn output(root: &Path, args: &[String]) -> Result<String> {
    let result = Command::new(&args[0])
        .args(&args[1..])
        .current_dir(root)
        .env_remove("GITHUB_OUTPUT")
        .output()
        .map_err(|e| e.to_string())?;
    if !result.status.success() {
        return Err(format!(
            "{}: {}",
            args.join(" "),
            String::from_utf8_lossy(&result.stderr)
        ));
    }
    String::from_utf8(result.stdout).map_err(|e| e.to_string())
}
fn plan(root: &Path, base: &str, paths: &[String]) -> Result<(String, Vec<Step>)> {
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
                let mut args = vec![
                    "cargo".into(),
                    verb.into(),
                    "--manifest-path".into(),
                    workspace.clone(),
                    "-p".into(),
                    name.clone(),
                ];
                args.extend(tail.into_iter().map(String::from));
                if verb == "test" && workspace.starts_with("rust/worker/") {
                    let mut wrapper: Vec<String> = [
                        "cargo",
                        "run",
                        "--manifest-path",
                        "rust/Cargo.toml",
                        "-p",
                        "pg-spawnd",
                        "--bin",
                        "ci-temporal",
                        "--",
                    ]
                    .into_iter()
                    .map(String::from)
                    .collect();
                    wrapper.extend(args);
                    args = wrapper;
                }
                steps.push(Step {
                    label: format!("{name}/{verb}"),
                    args,
                });
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
        steps.push(affected);
    }
    Ok((selection, steps))
}

pub fn run(args: &[String]) -> i32 {
    match execute(args) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("fast-checks: {error}");
            1
        }
    }
}
fn execute(args: &[String]) -> Result<()> {
    let start = Instant::now();
    let root = PathBuf::from(
        output(
            Path::new("."),
            &["git".into(), "rev-parse".into(), "--show-toplevel".into()],
        )?
        .trim(),
    );
    let mut base = "origin/main".to_string();
    let mut dry = false;
    let mut files = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--plan" => dry = true,
            "--base" => {
                index += 1;
                base = args.get(index).ok_or("--base requires a revision")?.clone();
            }
            "--files" => {
                files = Some(args[index + 1..].to_vec());
                break;
            }
            flag => return Err(format!("unknown argument {flag}")),
        }
        index += 1;
    }
    let paths = match files {
        Some(files) if dry => files,
        Some(_) => {
            return Err(
                "--files is only supported with --plan; execution always checks the real diff"
                    .into(),
            )
        }
        None => {
            let merge_base = output(
                &root,
                &[
                    "git".into(),
                    "merge-base".into(),
                    base.clone(),
                    "HEAD".into(),
                ],
            )?;
            let tracked = output(
                &root,
                &[
                    "git".into(),
                    "diff".into(),
                    "--name-only".into(),
                    "--no-renames".into(),
                    "-z".into(),
                    merge_base.trim().into(),
                ],
            )?;
            let untracked = output(
                &root,
                &[
                    "git".into(),
                    "ls-files".into(),
                    "--others".into(),
                    "--exclude-standard".into(),
                    "-z".into(),
                ],
            )?;
            tracked
                .split('\0')
                .chain(untracked.split('\0'))
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect()
        }
    };
    let (selection, steps) = plan(&root, &base, &paths)?;
    print!("{selection}");
    if dry {
        for step in steps {
            println!("{}: {}", step.label, step.args.join(" "));
        }
        return Ok(());
    }
    replay::verify(&root, &base)?;
    // Git's diff guard does not report CRLF consistently across core.whitespace settings.
    for path in &paths {
        if path.ends_with(".rs") {
            if let Ok(bytes) = std::fs::read(root.join(path)) {
                if bytes.windows(2).any(|pair| pair == b"\r\n") {
                    return Err(format!(
                        "CRLF in Rust input {path}; normalize to LF before testing"
                    ));
                }
            }
        }
    }
    let mut failures = Vec::new();
    for step in steps {
        let tick = Instant::now();
        let mut command = Command::new(&step.args[0]);
        if step.label.ends_with("/test")
            || matches!(step.label.as_str(), "affected-tests" | "rust-test-env")
        {
            replay::scrub_test_env(&mut command);
        }
        if step.label == "pg-worker/test" {
            command
                .env("PG_JAIL", "off")
                .env("PG_TEST_ALLOW_UNJAILED", "1");
        } else {
            command.env(
                "PG_JAIL_BIN",
                jail_binary(&root, std::env::var_os("CARGO_TARGET_DIR")),
            );
        }
        let status = command
            .args(&step.args[1..])
            .current_dir(&root)
            .env("FILESIZE_REQUIRE_BASE", &base)
            .env("NEWCODE_BASE", &base)
            .env("CHANGELOG_BASE", &base)
            .status()
            .map_err(|e| e.to_string())?;
        println!(
            "{} wall seconds: {:.3}",
            step.label,
            tick.elapsed().as_secs_f64()
        );
        if !status.success() {
            failures.push(step.label);
            // Missing pytest is a precondition failure, not permission to skip Rust tests.
            break;
        }
    }
    println!(
        "fast-checks total wall seconds: {:.3}",
        start.elapsed().as_secs_f64()
    );
    if start.elapsed() > Duration::from_secs(300) {
        eprintln!("WARNING: fast-checks exceeded the 300 second budget");
        failures.push("budget".into());
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(format!("failed: {}", failures.join(", ")))
    }
}

fn jail_binary(root: &Path, target: Option<std::ffi::OsString>) -> PathBuf {
    root.join(
        target
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("rust/target")),
    )
    .join("debug")
    .join(format!("pg-jail{}", std::env::consts::EXE_SUFFIX))
}

#[test]
fn jail_binary_uses_the_built_helper_without_a_target_override() {
    let root = std::env::temp_dir();
    let suffix = format!("debug/pg-jail{}", std::env::consts::EXE_SUFFIX);
    assert_eq!(
        jail_binary(&root, None),
        root.join("rust/target").join(&suffix)
    );
    assert_eq!(
        jail_binary(&root, Some("cache".into())),
        root.join("cache").join(suffix)
    );
}
