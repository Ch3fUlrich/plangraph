//! Bounded local projection of CI selection; the selector remains authoritative.
use crate::fast_check_packages::packages;
#[path = "fast_check_replay.rs"]
mod replay;
use pg_worker_core::cargo_cache;
use pg_worker_core::fast_check_stamp as stamp;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

pub(super) type Result<T> = std::result::Result<T, String>;

#[path = "fast_check_fleet.rs"]
mod fleet;
#[path = "fast_check_worktree.rs"]
mod owned;
#[path = "fast_check_target.rs"]
mod target;
use target::{jail_binary, resolve_cargo_target, run_step};

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
    if args.first().map(String::as_str) == Some("--dispatch") {
        return fleet::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("--receipt-ref") {
        // The pre-push hook asks which receipt ref belongs to a lane ref: one mapping, in one place.
        let target = args.get(1).ok_or("--receipt-ref requires a ref")?;
        println!("{}", stamp::receipt_ref_for(target));
        return Ok(());
    }
    if args.first().map(String::as_str) == Some("--check-notes") {
        // The pre-push hook asks this of every receipt ref a push would publish.
        let commit = args.get(1).ok_or("--check-notes requires a commit")?;
        return stamp::notes_commit_green(Path::new("."), commit, None);
    }
    if args.first().map(String::as_str) == Some("--stamp-green") {
        // The push gate asks this of a lane head: exit 0 only for a green receipt of exactly that head.
        let head = args.get(1).ok_or("--stamp-green requires a head sha")?;
        if args.get(2).map(String::as_str) == Some("--notes-commit") {
            // A push's receipt ref: only a tree of well-formed receipts with a green one of `head`.
            let commit = args.get(3).ok_or("--notes-commit requires a commit")?;
            return stamp::notes_commit_green(Path::new("."), commit, Some(head));
        }
        return if stamp::green(Path::new("."), head) {
            Ok(())
        } else {
            Err(format!("no green fast-checks stamp for {head}"))
        };
    }
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
    let mut owned = false;
    let mut files = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--plan" => dry = true,
            "--owned-worktree" => owned = true,
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
    if owned {
        if dry || files.is_some() {
            return Err("--owned-worktree checks the committed HEAD and cannot be used with --plan or --files".into());
        }
        return owned::run(&root, args);
    }
    let paths = match files {
        Some(files) if dry => files,
        Some(_) => {
            return Err(
                "--files is only supported with --plan; execution always checks the real diff"
                    .into(),
            )
        }
        None => owned::diff_paths(&root, &base)?,
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
    let primary = cargo_cache::shared_cache_roots().map_err(|e| e.to_string())?;
    let target = resolve_cargo_target(&root, &primary[0], "fast-checks")?;
    let mut failures = Vec::new();
    let mut checks = Vec::new();
    for step in steps {
        let tick = Instant::now();
        let mut command = Command::new(&step.args[0]);
        if step.label.ends_with("/test")
            || matches!(step.label.as_str(), "affected-tests" | "rust-test-env")
        {
            replay::scrub_test_env(&mut command);
        }
        command
            .env("CARGO_TARGET_DIR", &target.path)
            .env("CARGO_INCREMENTAL", "0")
            .env("PG_JAIL_BIN", jail_binary(&target.path));
        if step.label == "pg-worker/test" {
            command
                .env("PG_JAIL", "off")
                .env("PG_TEST_ALLOW_UNJAILED", "1");
        }
        if step.label == "new-code-lang" {
            command.env("CLASS_SEARCH_BASE", &base);
        }
        command
            .args(&step.args[1..])
            .current_dir(&root)
            .env("FILESIZE_REQUIRE_BASE", &base)
            .env("NEWCODE_BASE", &base)
            .env("CHANGELOG_BASE", &base);
        let status = run_step(&mut command, &step.args[0], &target).map_err(|e| e.to_string())?;
        println!(
            "{} wall seconds: {:.3}",
            step.label,
            tick.elapsed().as_secs_f64()
        );
        checks.push(stamp::Check {
            label: step.label.clone(),
            rc: status.code().unwrap_or(-1),
            seconds: tick.elapsed().as_secs_f64(),
        });
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
    // The owned-worktree parent turns these lines into the head's receipt.
    if let Some(path) = std::env::var_os(owned::RESULTS_ENV) {
        let lines: String = checks.iter().map(|c| c.line() + "\n").collect();
        std::fs::write(path, lines).map_err(|e| e.to_string())?;
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(format!("failed: {}", failures.join(", ")))
    }
}
