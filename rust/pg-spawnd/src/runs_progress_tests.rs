use super::*;

#[test]
fn round_refusal_is_saved_before_launch_and_does_not_consume_a_round() {
    use pg_worker_core::run::{supervise::Config, supervisor::SupervisorCfg};
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.path().to_owned(),
        ..Config::default()
    });
    cfg.max_runs = 1;
    let runs = Runs::new(Arc::new(Supervisor::new(cfg).unwrap()));
    for index in 1..=8 {
        let source = dir.path().join(format!("verdict-{index}.json"));
        std::fs::write(
            &source,
            json!({"writer_run_id":format!("run-r{index}"),
            "item_key":"p:I", "pr":null, "verdict":"REQUEST_CHANGES",
            "findings":[]})
            .to_string(),
        )
        .unwrap();
        create(
            dir.path(),
            &format!("run-r{index}"),
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "writer_round":index, "round_verdict_file":source}),
            Status::Completed,
        )
        .ok()
        .unwrap();
    }
    let args = json!({"card":{"item_key":"p:I"},"prompt":"p"});
    for _ in 0..2 {
        let result = runs
            .spawn_using(
                dir.path(),
                args.as_object().unwrap(),
                "fixture",
                json!("implement"),
                json!({}),
                |_, _| panic!("refused writer launched"),
            )
            .ok()
            .unwrap();
        assert_eq!(result["status"], "refused");
        assert_eq!(result["reason"], "round_cap");
        assert_eq!(result["item_key"], "p:I");
        assert!(result["writer_round"].is_null());
        let saved = crate::records::read_record(dir.path(), &result["run_id"])
            .ok()
            .unwrap();
        assert_eq!(saved["reason"], "round_cap");
    }
}

#[test]
fn a_dry_run_round_nine_refusal_through_the_spawn_path_queues_no_replan() {
    use pg_worker_core::run::{supervise::Config, supervisor::SupervisorCfg};
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.path().to_owned(),
        ..Config::default()
    });
    cfg.max_runs = 1;
    let runs = Runs::new(Arc::new(Supervisor::new(cfg).unwrap()));
    for index in 1..=8 {
        create(
            dir.path(),
            &format!("run-r{index}"),
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "round_pr":"https://forge/p/1",
            "writer_round":index, "round_cause":"red_ci", "round_failing_checks":3,
            "round_verdict_file":null}),
            Status::Completed,
        )
        .ok()
        .unwrap();
    }
    let args = json!({"card":{"item_key":"p:I", "pr":"https://forge/p/1",
        "round_cause":"red_ci", "failing_checks":3}, "prompt":"p", "dry_run":true});
    let result = runs
        .spawn_using(
            dir.path(),
            args.as_object().unwrap(),
            "fixture",
            json!("implement"),
            json!({}),
            |_, _| panic!("refused writer launched"),
        )
        .ok()
        .unwrap();
    assert_eq!(result["status"], "refused");
    assert_eq!(result["reason"], "round_cap");
    assert!(result.get("replan").is_none());
    assert!(!dir.path().join("replans").exists());
}

#[test]
fn the_writer_resumed_after_a_replan_gets_its_slices_in_the_prompt() {
    use pg_worker_core::run::{supervise::Config, supervisor::SupervisorCfg};
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.path().to_owned(),
        ..Config::default()
    });
    cfg.max_runs = 1;
    let runs = Runs::new(Arc::new(Supervisor::new(cfg).unwrap()));
    for index in 1..=3 {
        create(
            dir.path(),
            &format!("run-r{index}"),
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "round_pr":"https://forge/p/1",
            "writer_round":index, "round_cause":"red_ci", "round_failing_checks":3,
            "round_verdict_file":null}),
            Status::Completed,
        )
        .ok()
        .unwrap();
    }
    let args = json!({"card":{"item_key":"p:I", "pr":"https://forge/p/1",
        "round_cause":"red_ci", "failing_checks":3}, "prompt":"original prompt"});
    let spawn = || {
        let seen = std::cell::RefCell::new(String::new());
        let result = runs.spawn_using(
            dir.path(),
            args.as_object().unwrap(),
            "fixture",
            json!("implement"),
            json!({}),
            |_, request| {
                *seen.borrow_mut() = request["prompt"].as_str().unwrap_or("").to_string();
                Err(crate::errors::ToolError::Internal(
                    "stop before launch".into(),
                ))
            },
        );
        (result, seen.into_inner())
    };
    // Held while the replan is pending: the launch adapter is never reached.
    let (held, seen) = spawn();
    assert_eq!(held.ok().unwrap()["reason"], "round_replan_pending");
    assert_eq!(seen, "");
    // The replan reported: the same writer resumes with the slices after the original prompt.
    for path in std::fs::read_dir(dir.path().join("replans"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
    {
        let mut job: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        job["plan"] = json!({"leg":"strong", "slices":[
            {"key":"p:I-S1", "files":["a/one.rs"], "findings":["HIGH a/one.rs:3 keep"]}]});
        job["report"] = json!("sent");
        std::fs::write(path, job.to_string()).unwrap();
    }
    let (_, seen) = spawn();
    assert!(seen.starts_with("original prompt\n\n"));
    assert!(seen.contains("p:I-S1: a/one.rs"));
    assert!(seen.contains("Open finding: HIGH a/one.rs:3 keep"));
}
