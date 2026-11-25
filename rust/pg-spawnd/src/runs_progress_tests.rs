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

fn runs_with_rounds(dir: &Path, rounds: u64) -> Runs {
    use pg_worker_core::run::{supervise::Config, supervisor::SupervisorCfg};
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.to_owned(),
        ..Config::default()
    });
    cfg.max_runs = 1;
    for index in 1..=rounds {
        create(
            dir,
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
    Runs::new(Arc::new(Supervisor::new(cfg).unwrap()))
}

#[test]
fn round_nine_on_a_dry_run_through_the_spawn_path_queues_no_replan() {
    let dir = tempfile::tempdir().unwrap();
    let runs = runs_with_rounds(dir.path(), 8);
    let args = json!({"card":{"item_key":"p:I", "pr":"https://forge/p/1",
        "round_cause":"red_ci", "failing_checks":3}, "prompt":"p", "dry_run":true});
    let spawn = |_: &Path, _: &Value| panic!("refused writer launched");
    let result = runs
        .spawn_using(
            dir.path(),
            args.as_object().unwrap(),
            "fixture",
            json!("implement"),
            json!({}),
            spawn,
        )
        .ok()
        .unwrap();
    assert_eq!(result["reason"], "round_cap");
    assert!(result.get("replan").is_none() && !dir.path().join("replans").exists());
}

#[test]
fn the_writer_resumed_after_a_replan_gets_its_slices_in_the_prompt() {
    let dir = tempfile::tempdir().unwrap();
    let runs = runs_with_rounds(dir.path(), 3);
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
    let (held, seen) = spawn();
    assert_eq!(
        (held.ok().unwrap()["reason"].clone(), seen),
        (json!("round_replan_pending"), String::new())
    );
    for entry in std::fs::read_dir(dir.path().join("replans")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|x| x == "json") {
            let mut job: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
            job["plan"] = json!({"leg":"strong", "findings":["HIGH a/one.rs:3 keep"],
                "slices":[{"key":"p:I-S1", "files":["a/one.rs"]}]});
            job["report"] = json!("sent");
            std::fs::write(path, job.to_string()).unwrap();
        }
    }
    let (_, seen) = spawn();
    assert!(seen.starts_with("original prompt\n\n") && seen.contains("p:I-S1: a/one.rs"));
    assert!(seen.contains("Open finding: HIGH a/one.rs:3 keep"));
}

#[test]
fn an_initial_run_that_exits_without_changing_the_checkout_is_retried_through_the_launch_path() {
    use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};
    // (script, the next launch is the initial run again)
    for (script, retried) in [("exit 3", true), ("echo work >file; exit 3", false)] {
        let work = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| {
            let id = [
                ("GIT_AUTHOR_NAME", "F"),
                ("GIT_AUTHOR_EMAIL", "f@example.test"),
                ("GIT_COMMITTER_NAME", "F"),
                ("GIT_COMMITTER_EMAIL", "f@example.test"),
            ];
            let out = pg_worker_core::git_test_support::command(work.path())
                .args(args)
                .envs(id)
                .output()
                .unwrap();
            assert!(out.status.success());
        };
        git(&["init", "-q"]);
        git(&["commit", "-qm", "base", "--allow-empty"]);
        let state = tempfile::tempdir().unwrap();
        let cfg = SupervisorCfg::new(Config {
            state_dir: state.path().to_owned(),
            confinement: Confinement::Plain,
            ..Config::default()
        });
        let runs = Runs::new(Arc::new(Supervisor::new(cfg).unwrap()));
        let args = json!({"card":{"item_key":"p:I", "pr":"https://forge/p/1"}, "prompt":"p",
            "wait_seconds":10});
        let first = runs
            .spawn_using(
                state.path(),
                args.as_object().unwrap(),
                "fixture",
                json!("implement"),
                json!({"first_leg":{"family":"f"}}),
                |_, _| {
                    Ok(crate::launch::LaunchSpec {
                        argv: vec!["/bin/sh".into(), "-c".into(), script.into()],
                        cwd: work.path().to_owned(),
                        env: BTreeMap::new(),
                        stdin: Some("p".into()),
                        prompt: Some("p".into()),
                        warnings: None,
                        events: None,
                    })
                },
            )
            .ok()
            .unwrap();
        assert_eq!(
            (first["round_cause"].clone(), first["status"].clone()),
            (json!("initial"), json!("failed"))
        );
        // The record the launcher saved decides the next admission: no worked-on initial run
        // is repeated, an idle one is.
        let next = crate::round_progress::admit(state.path(), args.as_object().unwrap())
            .ok()
            .unwrap();
        assert_eq!(next.fields.get("writer_round").is_none(), retried);
    }
}
