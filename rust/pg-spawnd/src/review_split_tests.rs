use super::*;
use serde_json::json;

#[test]
fn groups_by_top_level_dir_and_packs_small_dirs_and_splits_big_dir() {
    let leg = json!({"tool_mode": "inline-only", "max_request_tokens": 200}); // 600 chars
    let files = vec![
        ("alpha/a1.rs".into(), vec![b'a'; 250]),
        ("alpha/a2.rs".into(), vec![b'b'; 250]),
        ("beta/b1.rs".into(), vec![b'c'; 100]),
        ("gamma/g1.rs".into(), vec![b'd'; 100]),
    ];
    let groups = plan_groups_from_measured(files, &leg, "review", true).unwrap();
    assert_eq!(
        groups,
        vec![
            vec!["alpha/a1.rs".to_string()],
            vec!["alpha/a2.rs".to_string()],
            vec!["beta/b1.rs".to_string(), "gamma/g1.rs".to_string()],
        ]
    );
}

#[test]
fn keeps_small_directory_together_even_across_many_files() {
    let leg = json!({"tool_mode": "inline-only", "max_request_tokens": 200}); // 600 chars
    let files = vec![
        ("alpha/a1.rs".into(), vec![b'a'; 50]),
        ("alpha/a2.rs".into(), vec![b'b'; 50]),
        ("beta/b1.rs".into(), vec![b'c'; 250]),
    ];
    let groups = plan_groups_from_measured(files, &leg, "review", true).unwrap();
    assert_eq!(
        groups,
        vec![
            vec!["alpha/a1.rs".to_string(), "alpha/a2.rs".to_string()],
            vec!["beta/b1.rs".to_string()],
        ]
    );
}

#[test]
fn single_file_exceeding_limit_refuses_naming_file() {
    let leg = json!({"tool_mode": "inline-only", "max_request_tokens": 120});
    let files = vec![
        ("small.rs".into(), vec![b'a'; 10]),
        ("crates/big/lib.rs".into(), vec![b'x'; 200]),
    ];
    let err = plan_groups_from_measured(files, &leg, "review", true).unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("prompt_too_large"), "{msg}");
    assert!(msg.contains("crates/big/lib.rs"), "{msg}");
}

#[test]
fn parse_verdict_ignores_fences_and_quotes_and_uses_last_line() {
    assert_eq!(
        parse_verdict_text(
            "```\nVERDICT: REQUEST_CHANGES\n```\n> VERDICT: FAIL\nVERDICT: ACCEPT\n"
        ),
        Some(SeatVerdict::Pass)
    );
    assert_eq!(
        parse_verdict_text("VERDICT: ACCEPT\nWait:\nVERDICT: REQUEST_CHANGES\n"),
        Some(SeatVerdict::RequestChanges)
    );
    assert_eq!(parse_verdict_text("VERDICT: pass | fix"), None);
    assert_eq!(parse_verdict_text("VERDICT: pass?"), None);
}

fn seat(status: &str, exit: i64, verdict: Option<&str>) -> Value {
    let mut r = json!({"run_id": "run-x", "status": status, "exit_code": exit,
                       "review_group": {"index": 1, "total": 2, "paths": ["a/x"]}});
    if let Some(v) = verdict {
        r["verdict"] = json!(v);
    }
    r
}

#[test]
fn aggregate_fails_the_review_when_any_group_does_not_pass() {
    let st = Path::new("/nonexistent");
    let pass = || seat("completed", 0, Some("PASS"));
    let all = aggregate_group_seats(st, "seats-a", &[pass(), pass()], false);
    assert_eq!(
        (all["status"].as_str(), all["verdict"].as_str()),
        (Some("completed"), Some("PASS"))
    );
    let changes = aggregate_group_seats(
        st,
        "seats-a",
        &[pass(), seat("completed", 0, Some("REQUEST_CHANGES"))],
        false,
    );
    assert_eq!(changes["status"], "failed");
    assert_eq!(changes["verdict"], "REQUEST_CHANGES");
    assert_eq!(changes["reason"], "review_request_changes");
    let halt = aggregate_group_seats(
        st,
        "seats-a",
        &[pass(), seat("completed", 0, Some("HALT"))],
        false,
    );
    assert_eq!(
        (halt["verdict"].as_str(), halt["reason"].as_str()),
        (Some("FAIL"), Some("review_failed"))
    );
    let exit = aggregate_group_seats(
        st,
        "seats-a",
        &[pass(), seat("completed", 3, Some("PASS"))],
        false,
    );
    assert_eq!(exit["verdict"], "FAIL");
    let pending = aggregate_group_seats(st, "seats-a", &[pass(), seat("running", 0, None)], false);
    assert_eq!(pending["status"], "running");
    assert_eq!(pending["passed"], false);
    let none = aggregate_group_seats(st, "seats-a", &[], false);
    assert_eq!(
        (none["status"].as_str(), none["passed"].as_bool()),
        (Some("failed"), Some(false))
    );
}

/// A later group whose launch is refused leaves the started group's run id in a failed seat group, and
/// its paths go to the fallback seats (which cannot be routed here, so the group fails, never passes).
#[cfg(unix)]
#[test]
fn a_refused_later_group_keeps_the_started_run_ids_and_never_reads_as_a_pass() {
    use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};
    let work = tempfile::tempdir().unwrap();
    let state = work.path().join("state");
    let runs = crate::runs::Runs::new(std::sync::Arc::new(
        pg_worker_core::run::supervisor::Supervisor::new(SupervisorCfg::new(Config {
            state_dir: state.clone(),
            confinement: Confinement::Plain,
            ..Config::default()
        }))
        .unwrap(),
    ));
    let leg = json!({"id":"fixture-leg","harness":"opencode","model":"m","family":"f"});
    let plan = json!({"first_leg": leg, "legs": [leg]});
    let args = json!({"role":"review","prompt":"review","wait_seconds":0,"card":{}})
        .as_object()
        .unwrap()
        .clone();
    let groups = vec![vec!["a/x".to_string()], vec!["b/y".to_string()]];
    let mut launched = 0;
    let mut launch = |_: &Path, _: &Value| {
        launched += 1;
        if launched == 2 {
            return Err(ToolError::Refused {
                kind: crate::errors::RefusalKind::Spawn,
                message: "quota".into(),
                legs: vec![],
            });
        }
        Ok(LaunchSpec {
            argv: vec!["/bin/sh".into(), "-c".into(), "cat >/dev/null".into()],
            cwd: work.path().into(),
            env: Default::default(),
            stdin: Some("review".into()),
            prompt: Some("review".into()),
            events: None,
            warnings: None,
        })
    };
    let answer = spawn_rust_split_groups(
        &runs,
        &state,
        &args,
        "local",
        &json!("review"),
        &plan,
        &leg,
        work.path(),
        false,
        &groups,
        &mut launch,
    )
    .ok()
    .unwrap();
    assert_eq!(answer["status"], "failed");
    assert_eq!(answer["passed"], false);
    assert_ne!(answer["verdict"], "PASS");
    assert_eq!(answer["seats"].as_array().unwrap().len(), 1, "{answer}");
    assert_eq!(answer["unstarted_groups"], 1);
}

/// A fallback seat that cannot start its own groups fails its group with the ids it started and does
/// not start the fallback pair again over the same paths.
#[cfg(unix)]
#[test]
fn a_fallback_seat_gets_one_transition_and_fails_its_group_with_its_started_ids() {
    use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};
    let work = tempfile::tempdir().unwrap();
    let state = work.path().join("state");
    let runs = crate::runs::Runs::new(std::sync::Arc::new(
        pg_worker_core::run::supervisor::Supervisor::new(SupervisorCfg::new(Config {
            state_dir: state.clone(),
            confinement: Confinement::Plain,
            ..Config::default()
        }))
        .unwrap(),
    ));
    let leg = json!({"id":"fixture-leg","harness":"opencode","model":"m","family":"f"});
    let plan = json!({"first_leg": leg, "legs": [leg]});
    let mut args = json!({"role":"review","prompt":"review","wait_seconds":0,"card":{}})
        .as_object()
        .unwrap()
        .clone();
    args.insert(crate::seat_fallback::FLAG.into(), json!(true));
    let groups = vec![vec!["a/x".to_string()], vec!["b/y".to_string()]];
    let mut launched = 0;
    let mut launch = |_: &Path, _: &Value| {
        launched += 1;
        if launched == 2 {
            return Err(ToolError::Refused {
                kind: crate::errors::RefusalKind::Spawn,
                message: "quota on the second group".into(),
                legs: vec![],
            });
        }
        Ok(LaunchSpec {
            argv: vec!["/bin/sh".into(), "-c".into(), "cat >/dev/null".into()],
            cwd: work.path().into(),
            env: Default::default(),
            stdin: Some("review".into()),
            prompt: Some("review".into()),
            events: None,
            warnings: None,
        })
    };
    let answer = spawn_rust_split_groups(
        &runs,
        &state,
        &args,
        "local",
        &json!("review"),
        &plan,
        &leg,
        work.path(),
        false,
        &groups,
        &mut launch,
    )
    .ok()
    .unwrap();
    // The failure is the group's own, not a failed attempt to route the fallback pair again.
    assert!(
        answer["reason"]
            .as_str()
            .unwrap()
            .contains("quota on the second group"),
        "{answer}"
    );
    assert_eq!(answer["seats"].as_array().unwrap().len(), 1);
    assert_eq!(answer["incomplete"], true);
    assert_eq!(answer["passed"], false);
}
