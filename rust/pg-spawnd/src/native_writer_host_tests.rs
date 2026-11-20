use super::*;
#[test]
fn final_head_review_ignores_old_head_and_requires_last_canonical_verdict() {
    let dir = std::env::temp_dir().join(format!("native-review-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("aaaaaaaa-hydrogen.txt"), "VERDICT: ACCEPT\n").unwrap();
    assert!(find_review(&dir, "bbbbbbbbbbbbbbbb").unwrap().is_none());
    fs::write(
        dir.join("bbbbbbbb-hydrogen.txt"),
        "VERDICT: ACCEPT\nVERDICT: UNKNOWN\n",
    )
    .unwrap();
    assert!(find_review(&dir, "bbbbbbbbbbbbbbbb").unwrap().is_none());
    fs::write(
        dir.join("bbbbbbbb-hydrogen.txt"),
        "VERDICT: REQUEST_CHANGES\n",
    )
    .unwrap();
    assert_eq!(
        find_review(&dir, "bbbbbbbbbbbbbbbb").unwrap().unwrap()["verdict"],
        "REQUEST_CHANGES"
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn terminal_writer_appends_ledger_with_log_tokens_once() {
    let dir = tempfile::tempdir().unwrap();
    let cwd = dir.path().join("worktree");
    fs::create_dir(&cwd).unwrap();
    let mut host = Host {
        repo: cwd.clone(),
        run_id: "run-fixture".into(),
        cwd,
        reviews: dir.path().join("reviews"),
        record_path: dir.path().join("record.json"),
        base_ref: "refs/heads/main".into(),
    };
    output(Command::new("git").arg("init").arg(&host.cwd)).unwrap();
    fs::write(
        dir.path().join("native.log"),
        "tokens used\n1,234\ntokens used: 56\n",
    )
    .unwrap();
    let record = json!({"run_id":"run-fixture", "host":"fixture", "started":0,
        "ended":120, "status":"failed", "exit_code":6, "resumes":1,
        "item_key":"plangraph:FIXTURE"});
    let mut running = record.clone();
    running.as_object_mut().unwrap().remove("ended");
    host.save(&running).unwrap();
    assert!(!dir.path().join("ledger.jsonl").exists());
    host.save(&record).unwrap();
    host.save(&record).unwrap();
    let text = fs::read_to_string(dir.path().join("ledger.jsonl")).unwrap();
    assert_eq!(text.lines().count(), 1);
    let row: Value = serde_json::from_str(text.trim()).unwrap();
    assert_eq!(row["host"], "fixture");
    assert_eq!(row["model"], WRITER_MODEL);
    assert_eq!(row["effort"], WRITER_EFFORT);
    let args = model_args();
    assert_eq!(args[1], row["model"].as_str().unwrap());
    assert!(args[3].ends_with(row["effort"].as_str().unwrap()));
    assert_eq!(row["tokens"], 1290);
    assert_eq!(row["wall_minutes"], 2.0);
    assert_eq!(row["items_given"], 1);
    assert_eq!(row["items_done"], 0);
    assert!(row["changed_lines"].is_null()); // No admitted base on this failure.
    fs::write(dir.path().join("native.log"), "tokens used: 1\n").unwrap();
    assert!(host.save(&record).is_err());
    let saved: Value =
        serde_json::from_str(&fs::read_to_string(&host.record_path).unwrap()).unwrap();
    assert_eq!(saved["ledger_error"], "terminal writer ledger row changed");
    assert_eq!(
        fs::read_to_string(dir.path().join("ledger.jsonl")).unwrap(),
        text
    );
}

#[test]
fn ledger_report_sums_runs_per_utc_day_and_marks_unknowns() {
    let dir = tempfile::tempdir().unwrap();
    for (id, ended, tokens) in [
        ("run-a", 120, json!(40)),
        ("run-b", 180, json!(null)),
        ("run-c", 86400, json!(2)),
    ] {
        let path = dir.path().join(id);
        fs::create_dir(&path).unwrap();
        fs::write(
            path.join("ledger.jsonl"),
            format!(
                "{}\n",
                json!({
                    "ended":ended, "tokens":tokens, "wall_minutes":2.0,
                    "items_given":1,"items_done":1,"changed_lines":3,
                    "review_rounds":1,"high_held":0,"med_held":0
                })
            ),
        )
        .unwrap();
    }
    let mut out = Vec::new();
    crate::cli::execute(
        vec!["ledger-report".into(), dir.path().as_os_str().to_owned()],
        &mut out,
        |_| panic!("report must not probe hosts"),
    )
    .unwrap();
    let rows: Vec<Value> = String::from_utf8(out)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["day"], "1970-01-01");
    assert_eq!(rows[0]["runs"], 2);
    assert_eq!(rows[0]["tokens"], 40);
    assert_eq!(rows[0]["tokens_unknown"], 1);
    assert_eq!(rows[0]["changed_lines"], 6);
    assert_eq!(rows[0]["wall_minutes"], 4.0);
    assert_eq!(rows[1]["day"], "1970-01-02");
}

#[test]
fn successful_writer_measures_committed_lines_items_and_json_usage() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = Host {
        repo: dir.path().into(),
        cwd: dir.path().into(),
        run_id: "run-success".into(),
        reviews: dir.path().join("reviews"),
        record_path: dir.path().join("record.json"),
        base_ref: "refs/heads/main".into(),
    };
    host.git(&["init"]).unwrap();
    let commit = |message: &str| {
        output(
            Command::new("git")
                .arg("-C")
                .arg(dir.path())
                .args(["commit", "-m", message])
                .env("GIT_AUTHOR_NAME", "fixture")
                .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
                .env("GIT_COMMITTER_NAME", "fixture")
                .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
        )
        .unwrap();
    };
    fs::write(dir.path().join("file"), "one\n").unwrap();
    host.git(&["add", "file"]).unwrap();
    commit("base");
    let base = host.head().unwrap();
    fs::write(dir.path().join("file"), "one\ntwo\nthree\n").unwrap();
    host.git(&["add", "file"]).unwrap();
    commit("fixture\n\nPlan-Item: plangraph:FIXTURE");
    let head = host.head().unwrap();
    fs::write(dir.path().join("native.log"), concat!(
        "{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":120,\"cached_input_tokens\":100,\"output_tokens\":9}}\n",
        "{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":10,\"output_tokens\":1}}\n"
    )).unwrap();
    let record = json!({"run_id":"run-success","started":60,"ended":240,"exit_code":0,
        "base_sha":base,"final_head":head,"item_key":"plangraph:FIXTURE",
        "review_rounds":1,"review":{"verdict":"ACCEPT"}});
    host.save(&record).unwrap();
    let row: Value =
        serde_json::from_str(&fs::read_to_string(dir.path().join("ledger.jsonl")).unwrap())
            .unwrap();
    assert_eq!(row["changed_lines"], 2);
    assert_eq!(row["items_done"], 1);
    assert_eq!(row["tokens"], 140);
    assert_eq!(row["review_rounds"], 1);
    assert_eq!(row["high_held"], 0);
    assert_eq!(row["med_held"], 0);
    assert_eq!(row["wall_minutes"], 3.0);
}
