use super::*;
use std::process::Command;

fn repository() -> (tempfile::TempDir, String) {
    let dir = tempfile::tempdir().ok().unwrap();
    git(dir.path(), &["init", "-q"]).ok().unwrap();
    std::fs::write(dir.path().join("caller.rs"), "review this caller")
        .ok()
        .unwrap();
    git(dir.path(), &["add", "."]).ok().unwrap();
    git(
        dir.path(),
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
    )
    .ok()
    .unwrap();
    let head = git(dir.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    let stamps = dir.path().join(".git/fast-checks");
    std::fs::create_dir(&stamps).ok().unwrap();
    std::fs::write(stamps.join(&head), "green").ok().unwrap();
    (dir, head)
}

#[test]
fn review_completion_requires_the_dispatched_diff() {
    let (repo, base) = repository();
    std::fs::write(repo.path().join("caller.rs"), "change for review").unwrap();
    let head = git(
        repo.path(),
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qam",
            "change",
        ],
    )
    .ok()
    .and_then(|_| git(repo.path(), &["rev-parse", "HEAD"]).ok())
    .unwrap();
    std::fs::write(repo.path().join(".git/fast-checks").join(&head), "green").unwrap();
    let mut launch = LaunchSpec {
        review_slot: None,
        argv: vec!["agy".into()],
        cwd: repo.path().into(),
        env: Default::default(),
        stdin: Some("review".into()),
        prompt: Some("review".into()),
        warnings: None,
        events: None,
    };
    git(
        repo.path(),
        &["update-ref", "refs/heads/review-base", &base],
    )
    .ok()
    .unwrap();
    let args = json!({"card":{"diff_ref":{"base":"refs/heads/review-base","head":"HEAD"}}});
    let leg = json!({"harness":"agy","model":"gemini-4-hydrogen-high"});
    let dispatch = prepare(args.as_object().unwrap(), &mut launch, &leg)
        .ok()
        .flatten()
        .unwrap();
    let expected = dispatch.binding.as_ref().unwrap();
    assert_eq!(expected["base_sha"], base);
    assert_eq!(expected["head_sha"], head);
    assert!(launch
        .prompt
        .unwrap()
        .contains(&format!("--no-textconv {base} {head} --")));
    let mut fallback = LaunchSpec {
        review_slot: None,
        argv: vec!["claude".into()],
        cwd: repo.path().into(),
        env: Default::default(),
        stdin: Some("review".into()),
        prompt: Some("review".into()),
        warnings: None,
        events: Some(vec![
            json!({"event":"sandbox_unavailable","host":"fixture-host"})
                .as_object()
                .unwrap()
                .clone(),
        ]),
    };
    let sonnet = json!({"harness":"claude","model":"claude-sonnet-5-5-high"});
    let read_only = prepare(args.as_object().unwrap(), &mut fallback, &sonnet)
        .ok()
        .flatten()
        .unwrap();
    assert_eq!(read_only.binding.as_ref(), Some(expected));
    let prompt = fallback.prompt.as_deref().unwrap();
    assert_eq!(fallback.stdin.as_deref(), Some(prompt));
    assert!(prompt.contains("+change for review"), "{prompt}");
    assert!(!prompt.contains("using `git"), "{prompt}");
    git(
        repo.path(),
        &["update-ref", "refs/heads/review-base", &head],
    )
    .ok()
    .unwrap();

    for (field, replacement) in [
        ("base_sha", json!(head)),
        ("head_sha", json!(base)),
        ("diff_sha256", json!("0".repeat(64))),
        ("", Value::Null),
    ] {
        let id = pg_worker_core::run::record::new_run_id();
        let mut metadata = json!({
            "role":"review", "cwd":repo.path(), "attempts":[{"leg":leg,"status":"running"}],
        });
        dispatch.apply(&mut metadata);
        if !field.is_empty() {
            metadata["review_binding"][field] = replacement;
        }
        let mut record =
            crate::runs::create(repo.path(), &id, "fixture", metadata, Status::Completed)
                .ok()
                .unwrap();
        let log = record.path().with_file_name("live.log");
        let write_review = |verdict| {
            let answer = format!("Reviewed caller.rs:1.\nVERDICT: {verdict}\nTEST_GAMING: no\n");
            std::fs::write(
                &log,
                format!(
                    "{}\n",
                    json!({"event":"result","result":{"status":"SUCCESS","response":answer}})
                ),
            )
            .unwrap();
        };
        write_review("PASS");
        record.set("live_log", json!(log));
        crate::runs::finish(&mut record, Status::Completed, &leg, 1);
        if field.is_empty() {
            assert_eq!(record.to_value()["completion"]["review_binding"], *expected);
            write_review("FAIL");
            crate::runs::finish(&mut record, Status::Completed, &leg, 1);
            assert_eq!(record.to_value()["verdict"], "pass");
        } else {
            assert!(record.to_value()["completion"].is_null(), "{field}");
            assert_eq!(
                record.to_value()["review_binding_error"],
                "review diff does not match dispatch"
            );
        }
    }
}

#[test]
fn agy_review_commands_run_in_remote_free_clone_at_stamped_detached_head() {
    let (repo, head) = repository();
    // Detached worktrees need not match the source repository's default branch.
    git(repo.path(), &["checkout", "--detach", "-q"])
        .ok()
        .unwrap();
    git(
        repo.path(),
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "detached",
        ],
    )
    .ok()
    .unwrap();
    let detached = git(repo.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    std::fs::write(
        repo.path().join(".git/fast-checks").join(&detached),
        "green",
    )
    .ok()
    .unwrap();
    let mut launch = LaunchSpec {
        review_slot: None,
        argv: vec![
            "agy".into(),
            "--add-dir".into(),
            repo.path().display().to_string(),
        ],
        cwd: repo.path().into(),
        env: Default::default(),
        stdin: Some("review".into()),
        prompt: Some("review".into()),
        warnings: None,
        events: None,
    };
    let args = json!({"card":{"cwd":repo.path(), "head_sha":detached}});
    assert_eq!(
        prepare(
            args.as_object().unwrap(),
            &mut launch,
            &json!({"harness":"agy"})
        )
        .ok()
        .unwrap()
        .map(|review| review.head),
        Some(detached.clone())
    );
    let checkout = isolate(&mut launch, &json!({"harness":"agy"}), Some(&detached))
        .ok()
        .unwrap();
    assert_ne!(launch.cwd, repo.path());
    assert_eq!(
        git(&launch.cwd, &["rev-parse", "HEAD"]).ok().unwrap(),
        detached
    );
    assert_ne!(detached, head);
    assert!(git(&launch.cwd, &["remote"]).ok().unwrap().is_empty());
    assert!(!Command::new("git")
        .current_dir(&launch.cwd)
        .arg("push")
        .output()
        .ok()
        .unwrap()
        .status
        .success());
    assert_eq!(
        std::fs::read_to_string(launch.cwd.join("caller.rs"))
            .ok()
            .unwrap(),
        "review this caller"
    );
    assert!(launch.argv.iter().any(|s| s.contains("command(*)")));
    assert!(!launch.argv.iter().any(|s| s == "--add-dir"));
    std::fs::write(launch.cwd.join("caller.rs"), "clone edit")
        .ok()
        .unwrap();
    assert_eq!(
        std::fs::read_to_string(repo.path().join("caller.rs"))
            .ok()
            .unwrap(),
        "review this caller"
    );
    drop(checkout);
    assert!(!launch.cwd.exists());
}

#[test]
fn empty_agy_review_is_fail_with_denied_tool_diagnostics() {
    // Other adapter tests temporarily replace the route adapter process-wide.
    // Exercise real finish behavior without running their scripts by accident.
    const CHILD: &str = "PG_AGY_EMPTY_REVIEW_TEST_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "cli_review::isolation_tests::empty_agy_review_is_fail_with_denied_tool_diagnostics", "--nocapture"])
            .env(CHILD, "1")
            .env_remove("PG_SPAWND_ADAPTER")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    for (result, response, status, terminated) in [
        ("SUCCESS", "", Status::Completed, true),
        ("SUCCESS", "  \n", Status::Completed, true),
        ("ERROR", "", Status::Failed, true),
        ("ERROR", "", Status::Failed, false),
    ] {
        let state = tempfile::tempdir().ok().unwrap();
        let leg = json!({"harness":"agy", "model":"gemini-4-hydrogen-high"});
        let mut record = crate::runs::create(
            state.path(),
            "run-empty",
            "fixture",
            json!({"role":"review","attempts":[{"leg":leg}]}),
            status,
        )
        .ok()
        .unwrap();
        let log = record.path().with_file_name("live.log");
        std::fs::write(&log, format!("{}\n{}\n",
            json!({"event":"tool", "tool":"command", "input":{"command":"ls -la"}, "error":"permission denied"}),
            json!({"event":"result", "result":{"status":result, "response":response}}))).ok().unwrap();
        if !terminated {
            let text = std::fs::read_to_string(&log).unwrap();
            std::fs::write(&log, text.trim_end_matches('\n')).unwrap();
        }
        record.set("live_log", json!(log));
        record.set("stderr_tail", json!("no output produced"));
        crate::runs::finish(&mut record, status, &leg, 1);
        let value = record.to_value();
        assert_eq!(value["verdict"], "fail", "{value}");
        assert_ne!(value["witness_kind"], "cli_reported");
        let review = std::fs::read_to_string(record.path().with_file_name("seat/review.txt"))
            .ok()
            .unwrap();
        assert!(review.contains("VERDICT: FAIL"), "{review}");
        assert!(review.contains("ls -la"), "{review}");
        assert!(review.contains("permission denied"), "{review}");
        assert!(review.contains("no output produced"), "{review}");
    }
}

#[test]
fn non_native_and_dry_agy_reviews_keep_existing_halt_diagnostic() {
    let state = tempfile::tempdir().unwrap();
    for (model, status) in [
        ("other-model", "completed"),
        ("gemini-4-hydrogen-high", "dry_run"),
    ] {
        let record = json!({"run_id":"run-halt", "status":status,
            "attempts":[{"leg":{"harness":"agy", "model":model}}]});
        let text = crate::review_artifacts::review_text(state.path(), &record).unwrap();
        assert!(text.contains("VERDICT: HALT"), "{text}");
    }
}
