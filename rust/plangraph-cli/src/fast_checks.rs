//! Bounded local projection of CI selection; the selector remains authoritative.
#[path = "fast_check_replay.rs"]
mod replay;
#[path = "fast_check_plan.rs"]
mod selection;
#[path = "test_replay/gate.rs"]
mod test_replay;
use pg_worker_core::cargo_cache;
use selection::plan;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};
pub use test_replay::{replay_committed, ReplayReceipt};

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
        return owned::run(&root, &base);
    }
    let head = if dry && files.is_some() {
        String::new()
    } else {
        replay::resolve(&root, "HEAD")?
    };
    let base = if dry && files.is_some() {
        base
    } else {
        replay::resolve(&root, &base)?
    };
    let paths = match files {
        Some(files) if dry => files,
        Some(_) => {
            return Err(
                "--files is only supported with --plan; execution always checks the real diff"
                    .into(),
            )
        }
        None => owned::diff_paths(&root, &base, &head)?,
    };
    let (selection, steps) = plan(&root, &base, &paths)?;
    print!("{selection}");
    if dry {
        for step in steps {
            println!("{}: {}", step.label, step.args.join(" "));
        }
        return Ok(());
    }
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
    let receipt = replay_committed(&root, &base, &head, None)?;
    print!("{}", receipt.evidence());
    let primary = cargo_cache::shared_cache_roots().map_err(|e| e.to_string())?;
    let target = resolve_cargo_target(&root, &primary[0], "fast-checks")?;
    let mut failures = Vec::new();
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
        if let Some(path) = std::env::var_os("PLANGRAPH_FAST_CHECK_RECEIPT") {
            receipt.write(Path::new(&path))?;
        }
        Ok(())
    } else {
        Err(format!("failed: {}", failures.join(", ")))
    }
}
