use crate::launch::*;
use serde_json::{json, Value};
use std::{
    os::unix::fs::PermissionsExt,
    time::{Duration, Instant},
};

struct TestDir(std::path::PathBuf);
impl std::ops::Deref for TestDir {
    type Target = std::path::Path;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
fn fixture() -> (TestDir, Value) {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    let spec = json!({"argv":["sh"],"cwd":dir,"env":{},"stdin":null});
    (TestDir(dir), spec)
}
fn script(dir: &std::path::Path, body: &str) -> LaunchAdapter {
    let path = dir.join("launch");
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    LaunchAdapter(Some(path.into_os_string()), "python3".into())
}
#[test]
fn contract_table() {
    let (dir, valid) = fixture();
    for (key, value) in [
        ("argv", json!([])),
        ("argv", json!([1])),
        ("argv", json!(["-sh"])),
        ("argv", json!([""])),
        ("argv", json!(["sh\0"])),
        ("argv", json!(vec!["sh"; 1025])),
        ("cwd", json!(dir.join("missing"))),
        ("cwd", json!(1)),
        ("env", json!({"X":1})),
        ("env", json!({"X=Y":"v"})),
        ("env", json!({"":"v"})),
        ("env", json!({"X\0":"v"})),
        ("env", json!({"X":"v\0"})),
        ("stdin", json!(1)),
        ("stdin", json!("x".repeat(8 * 1024 * 1024 + 1))),
        ("warnings", json!([1])),
        ("events", json!([{}])),
        ("events", json!([{"event":1}])),
    ] {
        let mut value_spec = valid.clone();
        value_spec[key] = value;
        assert!(
            matches!(parse(&serde_json::to_vec(&value_spec).unwrap()), Err(crate::errors::ToolError::Internal(s)) if s == "launch adapter returned an invalid launch spec"),
            "{key}"
        );
    }
    let mut missing = valid.clone();
    let mut large_env = valid.clone();
    large_env["env"] = Value::Object(
        (0..1025)
            .map(|n| (format!("KEY{n}"), json!("value")))
            .collect(),
    );
    assert!(parse(&serde_json::to_vec(&large_env).unwrap()).is_err());
    missing.as_object_mut().unwrap().remove("stdin");
    assert!(parse(&serde_json::to_vec(&missing).unwrap()).is_err());
    let mut good = valid;
    good["warnings"] = json!(["warning"]);
    good["events"] = json!([{"event":"ready","extra":true}]);
    good["unknown"] = json!(true);
    let spec = parse(&serde_json::to_vec(&good).unwrap()).ok().unwrap();
    assert_eq!(spec.warnings.unwrap(), ["warning"]);
    assert_eq!(spec.events.unwrap()[0]["event"], "ready");
}
#[test]
fn request_pipe_and_exits() {
    let (dir, valid) = fixture();
    std::fs::write(dir.join("spec"), serde_json::to_vec(&valid).unwrap()).unwrap();
    let adapter = script(
        &dir,
        &format!("cat > '{0}/request'\ncat '{0}/spec'", dir.display()),
    );
    let request = json!({"run_id":"run","job_id":"job","role":"implement","dry_run":false,"leg":{},"prompt":" verbatim\n","card":{},"cwd":dir.0,"state":dir.0});
    assert!(adapter
        .request(&dir, &request, Duration::from_secs(1))
        .is_ok());
    let saved: Value =
        serde_json::from_slice(&std::fs::read(dir.join("request")).unwrap()).unwrap();
    assert_eq!(saved, request);
    assert_eq!(saved.as_object().unwrap().len(), 9);
    for (body, expected) in [
        (
            "echo 'SpawnRefused: nope' >&2; exit 2",
            "SpawnRefused: nope",
        ),
        ("exit 1", "launch adapter failed (exit 1)"),
        ("kill -9 $$", "launch adapter killed by a signal"),
        (
            "echo broken",
            "launch adapter returned an invalid launch spec",
        ),
    ] {
        let error = script(&dir, body)
            .request(&dir, &request, Duration::from_secs(1))
            .err()
            .unwrap()
            .into_error_data();
        assert_eq!(error.message, expected);
    }
}
#[test]
fn timeout_kills_group() {
    let (dir, _) = fixture();
    let adapter = script(
        &dir,
        &format!("echo $$ > '{}/pid'\nsleep 30 &\nwait", dir.display()),
    );
    let start = Instant::now();
    let error = adapter
        .request(&dir, &json!({}), Duration::from_millis(300))
        .err()
        .unwrap()
        .into_error_data();
    assert_eq!(error.message, "launch adapter timed out");
    assert!(start.elapsed() < Duration::from_secs(2));
    let pid: i32 = std::fs::read_to_string(dir.join("pid"))
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
}

#[test]
fn exited_adapter_closes_descendant_pipes() {
    let (dir, valid) = fixture();
    std::fs::write(dir.join("spec"), serde_json::to_vec(&valid).unwrap()).unwrap();
    let adapter = script(&dir, &format!("cat '{}/spec'\n(sleep 5 &)", dir.display()));
    let start = Instant::now();
    assert!(adapter
        .request(&dir, &json!({}), Duration::from_secs(1))
        .is_ok());
    assert!(start.elapsed() < Duration::from_millis(900));
}

#[test]
fn lookup_rules_and_missing_program() {
    let (dir, _) = fixture();
    let default = LaunchAdapter::from_lookup(&|_| None);
    assert!(default.0.is_none());
    assert_eq!(default.1, "python3");
    let adapter = LaunchAdapter::from_lookup(&|key| match key {
        "PG_SPAWND_LAUNCH_CMD" => Some("".into()),
        "PG_SPAWND_PYTHON" => Some(dir.join("missing").into_os_string()),
        _ => None,
    });
    assert!(adapter.0.is_none());
    assert_eq!(adapter.1, dir.join("missing").into_os_string());
    let relative = LaunchAdapter::from_lookup(&|_| Some("relative".into()));
    for (adapter, has_os_error) in [(adapter, true), (relative, false)] {
        let message = adapter
            .request(&dir, &json!({}), Duration::from_secs(1))
            .err()
            .unwrap()
            .into_error_data()
            .message;
        assert!(
            message.starts_with("launch adapter could not start"),
            "{message}"
        );
        assert_eq!(message.contains("os error"), has_os_error, "{message}");
    }
}

#[test]
fn refusal_redaction_truncation_and_empty_line() {
    let (dir, _) = fixture();
    let secret = ["sk-", &"x".repeat(40)].concat();
    let line = format!("{secret} {}", "z ".repeat(200));
    let expected: String = pg_worker_core::redact::redact(&line)
        .chars()
        .take(300)
        .collect();
    assert!(!expected.contains(&secret));
    assert_eq!(expected.chars().count(), 300);
    for (line, expected) in [
        (line.as_str(), expected.as_str()),
        ("\nignored", "launch refused"),
    ] {
        let adapter = script(&dir, &format!("printf '%s\\n' '{line}' >&2; exit 2"));
        assert_eq!(
            adapter
                .request(&dir, &json!({}), Duration::from_secs(1))
                .err()
                .unwrap()
                .into_error_data()
                .message,
            expected
        );
    }
}

fn ok(result: Result<Value, crate::errors::ToolError>) -> Value {
    match result {
        Ok(value) => value,
        Err(_) => panic!("with_head_writers refused"),
    }
}

fn repo_with_writer_trailer(trailer: Option<&str>) -> (TestDir, crate::launch::RepoMap, Value) {
    use pg_worker_core::git_test_support::command as git;
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    let run = |args: &[&str]| {
        let out = git(&dir).args(args).output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_owned()
    };
    run(&["init", "-q"]);
    run(&["config", "user.name", "Fixture"]);
    run(&["config", "user.email", "fixture@example.invalid"]);
    std::fs::write(dir.join("f"), "a\n").unwrap();
    run(&["add", "f"]);
    run(&["commit", "-qm", "base"]);
    let base = run(&["rev-parse", "HEAD"]);
    std::fs::write(dir.join("f"), "b\n").unwrap();
    let message = match trailer {
        Some(t) => format!("change\n\nWriter: {t}"),
        None => "change".to_owned(),
    };
    run(&["commit", "-qam", &message]);
    let head = run(&["rev-parse", "HEAD"]);
    let entry = pg_worker_core::hosts::RepoEntry {
        root: dir.to_string_lossy().into(),
        worktrees_root: dir.join("trees").to_string_lossy().into(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
        live_dir: None,
        health_checks: None,
    };
    let repos = [("demo".to_owned(), entry)].into_iter().collect();
    let request = json!({"role":"review","writer_families":["openai"],
        "card":{"diff_ref":{"repo_id":"demo","base_sha":base,"head_sha":head}}});
    (TestDir(dir), repos, request)
}

#[test]
fn a_registered_repository_review_adds_the_head_writer_trailer_families() {
    let (_dir, repos, request) =
        repo_with_writer_trailer(Some("gemini-4-hydrogen-high run-1 (writer)"));
    let out = ok(with_head_writers(&request, &repos));
    assert_eq!(
        out["writer_families"],
        json!(["openai", "gemini-4-hydrogen-high"])
    );
    // Idempotent: a family already named is not repeated.
    assert_eq!(ok(with_head_writers(&out, &repos)), out);
}

#[test]
fn a_head_without_trailer_or_a_card_without_registered_ref_is_unchanged() {
    let (_dir, repos, request) = repo_with_writer_trailer(None);
    assert_eq!(ok(with_head_writers(&request, &repos)), request);
    let plain = json!({"role":"review","writer_families":["openai"],
        "card":{"diff_ref":{"repo":"/x","base":"a","head":"b"}}});
    assert_eq!(ok(with_head_writers(&plain, &repos)), plain);
    let implement = json!({"role":"implement","card":{}});
    assert_eq!(ok(with_head_writers(&implement, &repos)), implement);
}

#[test]
fn an_unreadable_registered_head_refuses_instead_of_assuming_no_writer() {
    let (_dir, repos, mut request) =
        repo_with_writer_trailer(Some("gemini-4-hydrogen-high run-1 (writer)"));
    request["card"]["diff_ref"]["head_sha"] = json!("0".repeat(40));
    let error = with_head_writers(&request, &repos).unwrap_err();
    assert!(
        matches!(&error, crate::errors::ToolError::Refused { message, .. } if message.contains("cannot read the Writer trailer")),
        "refused with another error"
    );
}

#[test]
fn unresolved_registered_refs_refuse_without_echoing_or_starting_the_adapter() {
    let (dir, spec) = fixture();
    let marker = dir.join("started");
    let adapter = script(
        &dir,
        &format!("touch {}; printf '%s' '{}'", marker.display(), spec),
    );
    // An unknown repository id, a malformed ref and a non-ASCII sha (a byte slice at 12 would panic).
    for diff_ref in [
        json!({"repo_id":"nowhere","base_sha":"a".repeat(40),"head_sha":"b".repeat(40)}),
        json!({"repo_id":"nowhere"}),
        json!({"repo_id":"nowhere","base_sha":"a".repeat(40),"head_sha":"ééééééééééééééé"}),
    ] {
        let request =
            json!({"role":"review","writer_families":["openai"],"card":{"diff_ref":diff_ref}});
        let error = adapter
            .request(&dir, &request, Duration::from_secs(5))
            .err()
            .expect("refusal");
        match error {
            crate::errors::ToolError::Refused { message, .. } => {
                assert!(
                    !message.contains("ééé") && !message.contains(&"b".repeat(12)),
                    "{message}"
                );
            }
            _ => panic!("expected a refusal"),
        }
        assert!(
            !marker.exists(),
            "the adapter child started before the lookup"
        );
    }
}
