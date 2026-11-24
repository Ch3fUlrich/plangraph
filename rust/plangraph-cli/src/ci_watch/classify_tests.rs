use super::*;

fn no_fixes() -> BTreeSet<(String, RedClass)> {
    BTreeSet::new()
}

#[test]
fn rust_fmt_diff_is_fmt() {
    let log = "Diff in src/lib.rs:12:\n-fn a( ){}\n+fn a() {}\nError: Process completed with exit code 1.";
    assert_eq!(
        classify_job("Rust workspace on Linux: fmt, clippy, tests", log, false),
        RedClass::Fmt
    );
}

#[test]
fn clippy_with_applicable_lint_is_clippy_and_other_lint_is_code() {
    let applicable = "error: this expression creates a reference which is immediately dereferenced\n  = help: `-D clippy::needless-borrow`\n  #[deny(clippy::needless_borrow)]\nerror: could not compile `pg-x` (lib) due to 1 previous error";
    assert_eq!(classify_job("Rust", applicable, false), RedClass::Clippy);
    let other = applicable.replace("needless_borrow", "too_many_arguments");
    assert_eq!(classify_job("Rust", &other, false), RedClass::Code);
}

#[test]
fn missing_module_only_outside_a_python_job() {
    let log = "ModuleNotFoundError: No module named 'pytest'";
    assert_eq!(
        classify_job("Rust workspace on Linux", log, false),
        RedClass::MissingModule
    );
    assert_eq!(
        classify_job("Python tests (shard 1 of 3)", log, false),
        RedClass::Configuration
    );
}

#[test]
fn infrastructure_configuration_and_unknown() {
    assert_eq!(
        classify_job("x", "The runner has received a shutdown signal", false),
        RedClass::Infrastructure
    );
    assert_eq!(
        classify_job(
            "x",
            "Invalid workflow file: .github/workflows/tests.yml",
            false
        ),
        RedClass::Configuration
    );
    assert_eq!(
        classify_job("x", "something odd happened", false),
        RedClass::Unknown
    );
    assert_eq!(
        classify_job("x", "thread 'a' panicked at src/x.rs:1", false),
        RedClass::Code
    );
}

#[test]
fn file_size_trailer_and_flake() {
    assert_eq!(
        classify_job("File size limit (400 lines)", "src/a.rs: 410 lines", false),
        RedClass::FileSize
    );
    assert_eq!(
        classify_job(
            "Commit gate",
            "ci-commit-gate: abc: missing Writer trailer",
            false
        ),
        RedClass::MissingTrailer
    );
    assert_eq!(classify_job("x", "panicked", true), RedClass::Flake);
}

#[test]
fn run_class_is_mixed_when_jobs_differ() {
    assert_eq!(classify_run(&[RedClass::Fmt, RedClass::Fmt]), RedClass::Fmt);
    assert_eq!(
        classify_run(&[RedClass::Fmt, RedClass::Code]),
        RedClass::Mixed
    );
    assert_eq!(classify_run(&[]), RedClass::Unknown);
}

#[test]
fn mechanical_class_is_fixed_once_per_head_and_the_commit_names_the_job() {
    let mut fixed = no_fixes();
    let Action::AutoFix {
        command, trailer, ..
    } = decide(RedClass::Fmt, "pr-9", 77, false, &fixed)
    else {
        panic!("fmt is mechanical")
    };
    assert_eq!(command, ["cargo", "fmt", "--all"]);
    assert!(
        trailer.starts_with("Hand-Edit: ") && trailer.contains("job 77"),
        "{trailer}"
    );
    fixed.insert(("pr-9".into(), RedClass::Fmt));
    assert_eq!(
        decide(RedClass::Fmt, "pr-9", 78, false, &fixed),
        Action::FixWriter
    );
    assert!(matches!(
        decide(RedClass::Fmt, "pr-10", 79, false, &fixed),
        Action::AutoFix { .. }
    ));
    assert!(matches!(
        decide(RedClass::Clippy, "pr-9", 80, false, &fixed),
        Action::AutoFix { .. }
    ));
}

#[test]
fn flake_is_rerun_once_and_the_rest_get_a_writer() {
    assert_eq!(
        decide(RedClass::Flake, "pr-1", 1, false, &no_fixes()),
        Action::RerunOnce
    );
    assert_eq!(
        decide(RedClass::Flake, "pr-1", 1, true, &no_fixes()),
        Action::FixWriter
    );
    for class in [
        RedClass::Code,
        RedClass::Infrastructure,
        RedClass::Configuration,
        RedClass::Unknown,
        RedClass::Mixed,
        RedClass::FileSize,
    ] {
        assert_eq!(
            decide(class, "pr-1", 1, false, &no_fixes()),
            Action::FixWriter
        );
    }
}

#[test]
fn infrastructure_brief_starts_at_the_runner_not_the_code() {
    assert!(RedClass::Infrastructure
        .start_at()
        .contains("not the batch's code"));
    assert!(RedClass::Configuration.start_at().contains("workflow"));
}

#[test]
fn a_changelog_job_that_prints_the_skip_success_line_is_not_a_trailer_failure() {
    let log =
        "changelog: skip used by 4aba1166 (fixture text only)\nFAIL: docs map missing an entry";
    assert_eq!(
        classify_job("Changelog fragment and docs", log, false),
        RedClass::Unknown
    );
}

#[test]
fn a_log_that_names_failing_tests_is_code_even_with_a_network_phrase() {
    let log = "test net::tests::retry ... FAILED\nHTTP 503 from the mock server";
    assert_eq!(classify_job("Rust", log, false), RedClass::Code);
}

#[test]
fn the_brief_lines_carry_class_run_class_job_and_start() {
    let text = brief_lines("configuration", "mixed", 9);
    assert!(text.contains("Class: configuration (run: mixed)"));
    assert!(text.contains("Failed job id: 9") && text.contains("workflow"));
    assert_eq!(brief_lines("", "", 0), "");
}
