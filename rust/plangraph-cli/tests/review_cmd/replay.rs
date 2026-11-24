use super::*;
use serde_json::json;

#[test]
fn deferred_native_part_evidence_reaches_the_batch_review() {
    let (tmp, repo, _origin, base) = init_repo_with_origin();
    fs::write(repo.join("part.txt"), "reviewed part\n").unwrap();
    git(&repo, &["add", "part.txt"]);
    git(&repo, &["commit", "-qm", "part\n\nWriter: gpt run-fixture"]);
    let part = git(&repo, &["rev-parse", "HEAD"]);
    fs::write(repo.join("batch.txt"), "another batch change\n").unwrap();
    git(&repo, &["add", "batch.txt"]);
    git(&repo, &["commit", "-qm", "batch"]);
    let batch = git(&repo, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let record = state.join("runs/run-fixture/record.json");
    fs::create_dir_all(record.parent().unwrap()).unwrap();
    let evidence = json!({
        "id":"ac".repeat(16), "base":base, "head":part,
        "tests":[{
            "id":"tests/test_part.py::test_result", "path":"tests/test_part.py", "backend":"sqlite",
            "base":{"outcome":"failed","summary":"1 failed"},
            "head":{"outcome":"passed","summary":"1 passed"}
        }],
        "committed_base_claims":[]
    });
    let mut writer = json!({
        "run_id":"run-fixture","run_kind":"native-codex","role":"implement",
        "status":"completed","exit_code":0,"review_status":"deferred_to_batch_seat",
        "base_sha":base,"final_head":part,"review_base":{"base":base,"head":part},
        "fast_checks":{"base":base,"head":part,"status":"passed","replay":evidence},
        "deferred_review":{"head":part,"test_replay":evidence}
    });
    fs::write(&record, writer.to_string()).unwrap();
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "pass");
    let review = || {
        Command::new(plangraph_bin())
            .args(["review", "--repo"])
            .arg(&repo)
            .args(["--base", &base, "--head", &batch, "--state"])
            .arg(&state)
            .args(["--no-fetch", "--native-part", "run-fixture"])
            .env("PLANGRAPH_SPAWNER_CMD", &mock)
            .env("PYTHONPATH", repo_root().join("src"))
            .output()
            .unwrap()
    };
    let accepted = review();
    assert!(accepted.status.success(), "{accepted:?}");
    let calls = fs::read_to_string(&log).unwrap();
    let launched = calls
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .find(|line| line["verb"] == "spawn")
        .unwrap();
    assert_eq!(launched["writer_test_replays"], json!([evidence]));
    let prompt = launched["prompt"].as_str().unwrap();
    assert!(
        prompt.contains(&format!("Base: {base}\nHead: {part}\n")),
        "{prompt}"
    );
    assert!(prompt.contains(
        "tests/test_part.py::test_result [sqlite] base=failed (1 failed); head=passed (1 passed)"
    ));
    fs::remove_file(&log).unwrap();

    writer["deferred_review"]["test_replay"]["id"] = json!("bb".repeat(16));
    fs::write(&record, writer.to_string()).unwrap();
    let refused = review();
    assert!(!refused.status.success(), "{refused:?}");
    assert!(
        String::from_utf8_lossy(&refused.stderr).contains("no matching deferred replay receipt")
    );
    assert!(!log.exists());
}
