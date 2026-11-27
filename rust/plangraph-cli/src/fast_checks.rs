//! Bounded local projection of CI selection; the selector remains authoritative.
use crate::fast_check_packages::packages;
#[path = "fast_check_replay.rs"]
mod replay;
use pg_worker_core::cargo_cache;
use pg_worker_core::fast_check_stamp as stamp;
use stamp::Check;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

pub(super) type Result<T> = std::result::Result<T, String>;
const WALL_BUDGET: Duration = Duration::from_secs(300);

#[path = "fast_check_fleet.rs"]
mod fleet;
#[path = "fast_check_worktree.rs"]
mod owned;
#[path = "fast_check_target.rs"]
mod target;
use target::{jail_binary, resolve_cargo_target, run_step};

#[cfg(test)]
#[rustfmt::skip]
mod tests {
    use super::*;
    use pg_worker_core::{git::{git, git_bytes, git_env, git_stdin}, worktree::fast_check_stamp};
    const OVER_690: &str = "budget overrun wall_ns=690000000000 limit_s=300\n";
    const OVER_453: &str = "budget overrun wall_ns=453000000000 limit_s=300\n";
    fn note(root: &Path, tree: &str) -> String {
        let args = ["-c", "i18n.commitEncoding=utf-8", "commit-tree", tree, "-m", "fast-checks receipt"];
        let identity = [
            ("GIT_AUTHOR_NAME", "plangraph"), ("GIT_AUTHOR_EMAIL", "plangraph@localhost"),
            ("GIT_COMMITTER_NAME", "plangraph"), ("GIT_COMMITTER_EMAIL", "plangraph@localhost"),
        ];
        git_env(root, &args, &identity).unwrap()
    }
    #[test]
    fn a_checkout_without_the_plangraph_selector_runs_its_own_check_or_the_whitespace_guard() {
        let dir = tempfile::tempdir().unwrap();
        let (selection, steps) = foreign_plan(dir.path(), "origin/main").unwrap();
        assert!(selection.contains("repository_check=false"), "{selection}");
        assert_eq!(steps.len(), 1);
        assert_eq!(steps[0].args, ["git", "diff", "--check", "origin/main...HEAD"]);
        std::fs::create_dir_all(dir.path().join(".plangraph")).unwrap();
        std::fs::write(dir.path().join(".plangraph/fast-checks"), "#!/bin/sh\n").unwrap();
        let (selection, steps) = foreign_plan(dir.path(), "origin/main").unwrap();
        assert!(selection.contains("repository_check=true"), "{selection}");
        assert_eq!(steps[0].args, [".plangraph/fast-checks", "origin/main"]);
        std::fs::create_dir_all(dir.path().join("tools")).unwrap();
        std::fs::write(dir.path().join("tools/ci_select.py"), "").unwrap();
        assert!(foreign_plan(dir.path(), "origin/main").is_none());
    }
    fn repo() -> (tempfile::TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        git(dir.path(), &["init"]).unwrap();
        let head = note(dir.path(), &git(dir.path(), &["write-tree"]).unwrap());
        (dir, head)
    }
    fn check(root: &Path, head: &str, wall: u64, rc: i32, budget: &str) {
        let path = root.join("results");
        let rows = [Check { label: "affected-tests".into(), rc, seconds: 1.25 }];
        let failures = (rc != 0).then(|| rows[0].label.clone());
        let result = finish(Duration::from_secs(wall), &rows, failures.as_slice(), Some(&path));
        assert_eq!(result.as_ref().err().map(String::as_str), (rc != 0).then_some("failed: affected-tests"));
        let steps = format!("check affected-tests rc={rc} wall=1.250\n{budget}");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), steps);
        assert_eq!(owned::settle(root, head, &path, owned::fixture_status(result.is_ok())).is_ok(), rc == 0);
        let text = format!("head {head}\nresult {}\n{steps}", if rc == 0 { "green" } else { "red" });
        assert_eq!(std::fs::read_to_string(fast_check_stamp(root, head).unwrap()).unwrap(), text);
        let tip = stamp::local_receipt_ref(head);
        assert_eq!(git_bytes(root, &["show", &format!("{tip}:{head}")]).unwrap(), text.as_bytes());
        assert_eq!(stamp::green(root, head), rc == 0);
        assert_eq!(stamp::notes_commit_green(root, &tip, Some(head)).is_ok(), rc == 0);
    }
    macro_rules! cases {
        ($($name:ident: $rc:literal, $($wall:literal => $budget:expr),+;)+) => {$(
            #[test]
            fn $name() { let (dir, head) = repo(); $(check(dir.path(), &head, $wall, $rc, $budget);)+ }
        )+};
    }
    cases! {
        green_over_budget_publishes_green_receipt_with_overrun: 0, 690 => OVER_690;
        failed_over_budget_has_no_green_receipt: 7, 453 => OVER_453;
        failed_under_budget_has_no_green_receipt: 7, 24 => "";
        green_under_budget_keeps_receipt_format: 0, 24 => "", 300 => "";
        overrun_rerun_replaces_head_report: 0, 453 => OVER_453, 490 => "budget overrun wall_ns=490000000000 limit_s=300\n";
    }
    #[test]
    fn notes_validators_accept_only_canonical_overrun() {
        let (dir, head) = repo();
        let root = dir.path();
        check(root, &head, 690, 0, OVER_690);
        let tip = stamp::local_receipt_ref(&head);
        assert!(stamp::notes_commit_green(root, &tip, None).is_ok());
        assert!(stamp::notes_commit_green(root, &tip, Some(&"b".repeat(40))).is_err());
        for tail in ["".into(), "budget overrun\n".into(), OVER_690.repeat(2),
            OVER_690.replace("690", "300"), OVER_690.replace("690", "299"),
            OVER_690.replace("300\n", "300 \n")] {
            let text = format!("head {head}\nresult green\ncheck fmt rc=0 wall=1.000\n{tail}");
            let blob = git_stdin(root, &["hash-object", "-w", "--stdin"], &text).unwrap();
            let tree = git_stdin(root, &["mktree"], &format!("100644 blob {blob}\t{head}\n")).unwrap();
            assert_eq!(stamp::text_is_green(&text, &head), tail.is_empty(), "{tail:?}");
            assert_eq!(stamp::notes_commit_green(root, &note(root, &tree), None).is_ok(), tail.is_empty());
        }
    }
}

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
/// A checkout without plangraph's selector (another project's repository) runs its own
/// declared check `.plangraph/fast-checks <base>` as the one step; without one only the
/// whitespace guard runs and the selection says so, so a native writer there still reaches its
/// review and the repository's own CI judges the pull request.
fn foreign_plan(root: &Path, base: &str) -> Option<(String, Vec<Step>)> {
    if root.join("tools/ci_select.py").is_file() {
        return None;
    }
    if root.join(".plangraph/fast-checks").is_file() {
        let steps = vec![step("repository-check", &[".plangraph/fast-checks", base])];
        return Some(("foreign=true\nrepository_check=true\n".into(), steps));
    }
    let range = format!("{base}...HEAD");
    let steps = vec![step("diff-check", &["git", "diff", "--check", &range])];
    let selection = "foreign=true\nrepository_check=false\nWARNING: no .plangraph/fast-checks in \
        this repository: only git diff --check runs\n";
    Some((selection.into(), steps))
}
fn plan(root: &Path, base: &str, paths: &[String]) -> Result<(String, Vec<Step>)> {
    if let Some(foreign) = foreign_plan(root, base) {
        return Ok(foreign);
    }
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
    let results = std::env::var_os(owned::RESULTS_ENV).map(PathBuf::from);
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
                .env_remove("PG_JAIL_BIN")
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
        let seconds = tick.elapsed().as_secs_f64();
        #[rustfmt::skip]
        println!(
            "{} wall seconds: {:.3}",
            step.label,
            seconds
        );
        checks.push(stamp::Check {
            label: step.label.clone(),
            rc: status.code().unwrap_or(-1),
            seconds,
        });
        if !status.success() {
            failures.push(step.label);
            // Missing pytest is a precondition failure, not permission to skip Rust tests.
            break;
        }
    }
    finish(start.elapsed(), &checks, &failures, results.as_deref())
}

fn finish(wall: Duration, checks: &[Check], failures: &[String], to: Option<&Path>) -> Result<()> {
    #[rustfmt::skip]
    println!(
        "fast-checks total wall seconds: {:.3}",
        wall.as_secs_f64()
    );
    let limit = WALL_BUDGET.as_secs();
    let budget = stamp::BudgetOverrun::new(wall.as_nanos(), limit).inspect(|_| {
        eprintln!("WARNING: fast-checks exceeded the {limit} second budget");
    });
    // The owned-worktree parent turns these lines into the head's receipt.
    if let Some(path) = to {
        let lines: String = checks.iter().map(|c| c.line() + "\n").collect();
        let lines = lines + &budget.map_or_else(String::new, |b| b.line() + "\n");
        std::fs::write(path, lines).map_err(|e| e.to_string())?;
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(format!("failed: {}", failures.join(", ")))
    }
}
