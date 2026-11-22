use super::{tests::git_ok, *};
use pg_worker_core::run::record::new_run_id;

#[test]
fn cli_review_completion_requires_base_head_and_diff_receipt_once() {
    let dir = tempfile::tempdir().unwrap();
    git_ok(dir.path(), &["init", "-q"]);
    git_ok(
        dir.path(),
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    );
    let head = git(dir.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    std::fs::write(dir.path().join("reviewed"), "the review change").unwrap();
    git_ok(dir.path(), &["add", "reviewed"]);
    git_ok(
        dir.path(),
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "change",
        ],
    );
    let base = head;
    let head = git(dir.path(), &["rev-parse", "HEAD"]).ok().unwrap();
    let diff = pg_worker_core::workspace_resolver::git_command(dir.path())
        .args(super::binding::DIFF_ARGS)
        .args([&base, &head, "--"])
        .output()
        .unwrap();
    assert!(diff.status.success());
    assert!(!diff.stdout.is_empty());
    use sha2::{Digest, Sha256};
    let binding = json!({
        "base_sha": base,
        "head_sha": head,
        "diff_sha256": format!("{:x}", Sha256::digest(&diff.stdout)),
    });
    let mut launch = LaunchSpec {
        argv: vec!["agy".into()],
        cwd: dir.path().into(),
        env: Default::default(),
        stdin: Some("review".into()),
        prompt: Some("review".into()),
        warnings: None,
        events: None,
    };
    let mut dispatch = json!({"head_sha": head});
    bind_launch(
        &mut launch,
        dir.path(),
        &json!({"base": "HEAD^"}),
        &mut dispatch,
    )
    .ok()
    .unwrap();
    assert_eq!(dispatch["review_base"], base);
    assert_eq!(dispatch["review_binding"], binding);
    assert!(launch.stdin.unwrap().contains(&format!("{base} {head} --")));
    let id = new_run_id();
    let leg = json!({"id":"agy-gemini-4-hydrogen-high", "harness":"agy", "model":"gemini-4-hydrogen-high", "family":"gemini"});
    let mut record = crate::runs::create(
        dir.path(),
        &id,
        "fixture",
        json!({
            "role":"review", "head_sha":head, "review_base":base, "cwd":dir.path(),
            "item_key":"demo:A", "exit_code":0,
            "attempts":[{"leg":leg,"status":"running"}]
        }),
        Status::Completed,
    )
    .ok()
    .unwrap();
    let log = record.path().with_file_name("live.log");
    let answer = "No findings in reviewed:1.\nVERDICT: PASS\nTEST_GAMING: no\n";
    std::fs::write(
        &log,
        format!(
            "{}\n",
            json!({"event":"result", "result":{"status":"SUCCESS", "response":answer}})
        ),
    )
    .unwrap();
    record.set("live_log", json!(log));
    for (field, wrong) in [
        ("base_sha", head.clone()),
        ("head_sha", base.clone()),
        ("diff_sha256", "0".repeat(64)),
    ] {
        let mut mismatch = binding.clone();
        mismatch[field] = json!(wrong);
        record.set("review_binding", mismatch);
        super::finish(&mut record, Status::Completed, &leg);
        assert!(record.get("completion").is_none(), "{field}");
    }
    record.set("review_binding", dispatch["review_binding"].clone());
    crate::runs::finish(&mut record, Status::Completed, &leg, 1);
    let value = record.to_value();
    assert_eq!(value["served_model"], "gemini-4-hydrogen-high");
    assert_eq!(value["served_family"], "google");
    assert_eq!(value["witness_kind"], "cli_reported");
    assert_eq!(value["completion"]["head_sha"], head);
    assert_eq!(value["completion"]["review_binding"], binding);
    std::fs::write(
        &log,
        format!(
            "{}\n",
            json!({"event":"result","result":{"status":"SUCCESS",
            "response":"New finding in reviewed:1.\nVERDICT: FAIL\nTEST_GAMING: no\n"}})
        ),
    )
    .unwrap();
    super::finish(&mut record, Status::Completed, &leg);
    assert_eq!(record.to_value()["verdict"], "pass");

    assert_eq!(value["verdict"], "pass");
    assert_eq!(value["findings"], "No findings in reviewed:1.");
    assert_eq!(value["attempts"][0]["witness_kind"], "cli_reported");
}
