#![cfg(unix)]
use pg_worker_core::remote_steps::{execute, parse_env_file};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

fn run(plan: Value) -> (i32, String) {
    let mut out = Vec::new();
    let code = execute(plan.to_string().as_bytes(), &mut out).unwrap();
    (code, String::from_utf8(out).unwrap())
}
fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(["-c", "user.name=T", "-c", "user.email=t@example.invalid"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{args:?}");
    String::from_utf8(out.stdout).unwrap().trim().into()
}

#[test]
fn env_file_lines_are_parsed_without_a_shell() {
    let pairs = parse_env_file("# note\nexport A=1\nB=\"two words\"\n\nC='x'\n").unwrap();
    assert_eq!(
        pairs,
        [("A", "1"), ("B", "two words"), ("C", "x")].map(|(k, v)| (k.to_owned(), v.to_owned()))
    );
    for bad in ["no equals", "1A=x", "A-B=x"] {
        assert!(parse_env_file(bad).is_err(), "{bad}");
    }
}

#[test]
fn env_file_reaches_every_step_a_step_env_overrides_it_and_a_missing_file_runs_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let env_file = dir.path().join("env");
    fs::write(&env_file, "FROM_FILE=file\nOVERRIDDEN=file\n").unwrap();
    let steps = json!([
        {"argv": ["printenv", "FROM_FILE"]},
        {"argv": ["printenv", "OVERRIDDEN"], "env": {"OVERRIDDEN": "step"}}]);
    let (code, out) = run(json!({"env_file": env_file, "steps": steps}));
    assert_eq!((code, out.as_str()), (0, "file\nstep\n"));
    let (code, out) = run(json!({"env_file": dir.path().join("absent"), "steps": steps}));
    assert_eq!((code, out.as_str()), (1, ""));
}

#[test]
fn cwd_is_the_working_directory_of_every_step_and_must_exist() {
    let dir = tempfile::tempdir().unwrap();
    let real = fs::canonicalize(dir.path()).unwrap();
    let (code, out) =
        run(json!({"cwd": dir.path(), "steps": [{"argv": ["pwd"]}, {"argv": ["pwd"]}]}));
    assert_eq!((code, out), (0, format!("{0}\n{0}\n", real.display())));
    let (code, out) = run(json!({"cwd": dir.path().join("absent"), "steps": [{"argv": ["pwd"]}]}));
    assert_eq!((code, out.as_str()), (1, ""));
}

#[test]
fn steps_stop_at_the_first_failure_unless_keep_going_and_the_first_code_is_kept() {
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join("after");
    let steps = json!([
        {"argv": ["ls", dir.path().join("absent")]},
        {"argv": ["false"]},
        {"argv": ["touch", marker]}]);
    let (code, _) = run(json!({"steps": steps}));
    assert!(code != 0 && !marker.exists());
    let first = code;
    let (code, _) = run(json!({"keep_going": true, "steps": steps}));
    assert_eq!(code, first);
    assert_ne!(first, 1, "the kept code is ls's, not false's");
    assert!(marker.exists());
}

#[test]
fn write_file_applies_the_exact_mode_text_and_hex_and_refuses_bad_input() {
    let dir = tempfile::tempdir().unwrap();
    let text = dir.path().join("card.json");
    let bin = dir.path().join("review.bundle");
    let (code, _) = run(json!({"steps": [
        {"write_file": {"path": text, "mode": 0o600}, "stdin_text": "{\"k\":1}"},
        {"write_file": {"path": bin, "mode": 0o600}, "stdin_hex": "00ff10"}]}));
    assert_eq!(code, 0);
    assert_eq!(fs::read_to_string(&text).unwrap(), "{\"k\":1}");
    assert_eq!(fs::read(&bin).unwrap(), [0, 255, 16]);
    for path in [&text, &bin] {
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    for step in [
        json!({"write_file": {"path": dir.path().join("no/dir/f"), "mode": 0o600}, "stdin_text": "x"}),
        json!({"write_file": {"path": text, "mode": 0o600}, "stdin_hex": "0"}),
        json!({"write_file": {"path": text, "mode": 0o600}, "stdin_hex": "zz"}),
    ] {
        assert_eq!(run(json!({"steps": [step]})).0, 1);
    }
}

#[test]
fn ensure_commit_passes_when_present_fetches_when_absent_and_fails_when_the_fetch_fails() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    let target = dir.path().join("target");
    for repo in [&source, &target] {
        fs::create_dir(repo).unwrap();
        git(repo, &["init", "-q", "-b", "main"]);
    }
    git(&source, &["commit", "-q", "--allow-empty", "-m", "one"]);
    let sha = git(&source, &["rev-parse", "HEAD"]);
    let plan = |fetch: Value| json!({"cwd": target, "steps": [{"ensure_commit": {"sha": sha, "fetch_argv": fetch}}]});
    let broken = json!(["git", "fetch", "-q", dir.path().join("nowhere"), "main"]);
    assert_ne!(run(plan(broken.clone())).0, 0);
    let good = json!(["git", "fetch", "-q", &source, "main"]);
    assert_eq!(run(plan(good)).0, 0);
    // Present now: the broken fetch is never reached.
    assert_eq!(run(plan(broken)).0, 0);
}

#[test]
fn receipt_brackets_the_step_output_and_the_plan_exits_with_the_step_code() {
    let (code, out) = run(json!({"steps": [
        {"argv": ["echo", "before"]},
        {"argv": ["echo", "{\"run_id\":\"run-1\"}"], "receipt": {"begin": "BEGIN-x", "end": "END-x"}}]}));
    assert_eq!(
        (code, out.as_str()),
        (0, "before\nBEGIN-x\n{\"run_id\":\"run-1\"}\n\nEND-x\n")
    );
    let (code, out) = run(json!({"steps": [
        {"argv": ["ls", "/nonexistent-for-steps"], "receipt": {"begin": "B", "end": "E"}},
        {"argv": ["echo", "never"]}]}));
    assert_ne!(code, 0);
    assert_eq!(out, "B\n\nE\n");
}

#[test]
fn unknown_plan_fields_and_an_action_less_step_are_refused() {
    let mut out = Vec::new();
    assert!(execute(r#"{"steps":[],"shell":"x"}"#.as_bytes(), &mut out).is_err());
    assert_eq!(run(json!({"steps": [{}]})).0, 1);
}

#[test]
fn fail_code_replaces_a_failing_steps_code_and_a_leading_tilde_means_home() {
    let (code, _) = run(
        json!({"steps": [{"argv": ["test", "-d", "/nonexistent-for-steps"], "fail_code": 44}]}),
    );
    assert_eq!(code, 44);
    let (code, _) = run(json!({"steps": [{"argv": ["true"], "fail_code": 44}]}));
    assert_eq!(code, 0);
    let home = tempfile::tempdir().unwrap();
    fs::write(home.path().join("marker"), "").unwrap();
    std::env::set_var("HOME", home.path());
    let (code, out) = run(json!({"cwd": "~/", "steps": [
        {"argv": ["test", "-f", "~/marker"]}, {"argv": ["printenv", "X"], "env": {"X": "~/marker"}}]}));
    assert_eq!(code, 0);
    assert_eq!(out, format!("{}/marker\n", home.path().display()));
}

#[test]
fn a_step_cwd_replaces_the_plan_cwd_for_that_step_only() {
    let plan = tempfile::tempdir().unwrap();
    let (code, out) = run(json!({"cwd": plan.path(), "steps": [
        {"argv": ["pwd"], "cwd": "/"}, {"argv": ["pwd"]}]}));
    assert_eq!(code, 0);
    assert_eq!(
        out,
        format!("/\n{}\n", fs::canonicalize(plan.path()).unwrap().display())
    );
}
