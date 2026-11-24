use super::*;

#[test]
fn final_head_review_ignores_old_head_and_requires_last_canonical_verdict() {
    let head = "b".repeat(40);
    let paths = vec!["src/lib.rs".to_owned()];
    let old = format!(
        "src/lib.rs fine\nHEAD: {}\nVERDICT: ACCEPT\n",
        "a".repeat(40)
    );
    assert_eq!(review_verdict(&old, &head, &paths)["verdict"], "no_verdict");
    let unknown = "src/lib.rs\nVERDICT: ACCEPT\nVERDICT: UNKNOWN\n";
    assert_eq!(
        review_verdict(unknown, &head, &paths)["verdict"],
        "no_verdict"
    );
    let weak = format!("HEAD: {head}\nVERDICT: ACCEPT\n");
    assert_eq!(
        review_verdict(&weak, &head, &paths)["reason"],
        "weak_verdict"
    );
    let changes = format!("HIGH src/lib.rs:3 - x\nHEAD: {head}\nVERDICT: REQUEST_CHANGES\n");
    let verdict = review_verdict(&changes, &head, &paths);
    assert_eq!(verdict["verdict"], "REQUEST_CHANGES");
    assert_eq!(verdict["head"], head.as_str());
    let accept = format!("FILES READ: src/lib.rs:3\nHEAD: {head}\nVERDICT: ACCEPT\n");
    assert_eq!(review_verdict(&accept, &head, &paths)["verdict"], "ACCEPT");
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
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    output(Command::new("git").arg("init").arg(&host.cwd)).unwrap();
    fs::write(
        dir.path().join("native.log"),
        "tokens used\n1,234\ntokens used: 56\n",
    )
    .unwrap();
    let record = json!({"run_id":"run-fixture", "host":"fixture", "started":0,
        "ended":120, "status":"failed", "exit_code":6, "resumes":1,
        "item_key":"plangraph:FIXTURE", "run_kind":"native-codex", "role":"implement",
        "requested_model": WRITER_MODEL, "plan":{"effort":{"level":"L2","reason":"size_bucket"}}});
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
    let args = model_args(&host.effort);
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
    assert_eq!(saved["attempt_capture"], 1);
    let attempt = crate::attempt_outcome::project(saved).unwrap();
    assert_eq!(attempt["requested_model"], WRITER_MODEL);
    assert_eq!(attempt["level"], "L2");
    assert_eq!(
        fs::read_to_string(dir.path().join("ledger.jsonl")).unwrap(),
        text
    );
    let journal = dir.path().join("run-lifecycle");
    fs::write(journal.join("published"), b"1").unwrap();
    fs::write(&host.record_path, serde_json::to_vec(&running).unwrap()).unwrap();
    let page = lifecycle::page(dir.path(), 1).unwrap();
    assert_eq!(page["rows"][0]["status"], "failed");
    let recovered: Value = serde_json::from_slice(&fs::read(&host.record_path).unwrap()).unwrap();
    assert_eq!(recovered["ended"], 120);
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
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
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

#[test]
fn stored_accept_without_reviewed_file_is_no_verdict() {
    let dir = tempfile::tempdir().unwrap();
    let prefix = "I am running tests in the background.\n";
    let tail = "\nFINDING none\nVERDICT: ACCEPT\n";
    let text = format!(
        "{prefix}{}{tail}",
        " ".repeat(288 - prefix.len() - tail.len())
    );
    assert_eq!(text.len(), 288);
    let review = review_verdict(&text, "aaaaaaaaaaaaaaaa", &[]);
    let _ = &dir;
    assert_eq!(review["status"], "no_verdict");
    assert_eq!(review["reason"], "weak_verdict");
    assert_ne!(review["verdict"], "ACCEPT");
}

#[test]
fn accept_requires_a_file_read_and_rejects_claimed_toollessness() {
    let paths = ["src/lib.rs".into()];
    for review in [
        "Reviewed only the inline diff for src/lib.rs.\nVERDICT: ACCEPT\n",
        "I had no repository, shell, or test tool available.\nFILES READ: src/lib.rs\nVERDICT: ACCEPT\n",
        "FILES READ: none\nReviewed src/lib.rs.\nVERDICT: ACCEPT\n",
    ] {
        assert_eq!(review_verdict(review, "head", &paths)["verdict"], "no_verdict");
    }
}

#[test]
fn stored_accept_with_changed_path_or_file_line_is_accepted() {
    let dir = tempfile::tempdir().unwrap();
    for citation in ["Reviewed src/lib.rs", "Reviewed Makefile:12"] {
        let text = format!("FILES READ: {citation}\nFINDING none\nVERDICT: ACCEPT\n");
        let _ = &dir;
        assert_eq!(
            review_verdict(&text, "aaaaaaaaaaaaaaaa", &["src/lib.rs".into()])["verdict"],
            "ACCEPT"
        );
    }
}

#[test]
fn native_weak_review_exits_as_missing_verdict_without_running_gates() {
    /// The writer commits during its first turn: HEAD is the base until `execute` ran.
    struct Weak(bool);
    impl crate::native_writer::Driver for Weak {
        fn prepare(&mut self) -> Result<String, String> {
            Ok("base".into())
        }
        fn refresh_base(&mut self) -> Result<String, String> {
            panic!("weak review must not relaunch the writer")
        }
        fn disk_gb(&mut self) -> Result<u64, String> {
            Ok(5)
        }
        fn execute(&mut self, session: Option<&str>, _: &str) -> Result<String, String> {
            assert!(session.is_none(), "weak review must not resume the writer");
            self.0 = true;
            Ok("session".into())
        }
        fn head(&mut self) -> Result<String, String> {
            Ok(if self.0 { "head" } else { "base" }.into())
        }
        fn has_commits_beyond(&mut self, base: &str, head: &str) -> Result<bool, String> {
            Ok(base != head)
        }
        fn fast_checks(&mut self, _: &str) -> Result<(), String> {
            Ok(())
        }
        fn stamped(&mut self, _: &str) -> bool {
            true
        }
        fn review(&mut self, head: &str, _: Option<&Value>) -> Result<Value, String> {
            Ok(
                json!({"status":"no_verdict", "verdict":"no_verdict", "reason":"weak_verdict", "head":head}),
            )
        }
        fn gates(&mut self) -> Result<(), String> {
            panic!("weak review cannot authorize final gates")
        }
        fn save(&mut self, _: &Value) -> Result<(), String> {
            Ok(())
        }
    }
    let record = crate::native_writer::run(&mut Weak(false), json!({}), "brief");
    assert_eq!(record["exit_code"], 4);
    let reason = record["reason"].as_str().unwrap();
    assert!(reason.starts_with("weak_verdict") && reason.ends_with(": weak_verdict"));
    assert!(record["review"].is_null());
}

#[test]
fn writer_effort_is_medium_unless_a_decision_names_a_higher_one() {
    assert_eq!(WRITER_MODEL, "gpt-6.2-sol");
    assert_eq!(writer_effort(&json!({})).unwrap(), "medium");
    assert_eq!(
        writer_effort(&json!({"effort":"medium"})).unwrap(),
        "medium"
    );
    assert_eq!(
        writer_effort(&json!({"effort":"xhigh","effort_decision":"D-6397"})).unwrap(),
        "xhigh"
    );
    for card in [
        json!({"effort":"high"}),
        json!({"effort":"high","effort_decision":"because"}),
        json!({"effort":"high","effort_decision":"D-"}),
    ] {
        assert!(writer_effort(&card)
            .unwrap_err()
            .starts_with("effort_needs_decision"));
    }
    assert!(writer_effort(&json!({"effort":"low"}))
        .unwrap_err()
        .starts_with("effort_invalid"));
    assert_eq!(model_args("high")[3], "model_reasoning_effort=high");
    assert_eq!(model_args(WRITER_EFFORT)[5], "model_context_window=1000000");
}

#[path = "native_writer_replay_card_tests.rs"]
mod replay_card;

#[cfg(unix)]
#[test]
fn card_refusals_exit_five_before_codex_starts() {
    for (card, reason) in [
        (json!({"effort":"high"}), "effort_needs_decision"),
        (json!({"effort":"low"}), "effort_invalid"),
        (json!({"push_ref":"main"}), "push_ref must name refs/heads/"),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let record_path = dir.path().join("state/runs/run-card/record.json");
        fs::create_dir_all(record_path.parent().unwrap()).unwrap();
        let request = dir.path().join("request.json");
        fs::write(
            &request,
            json!({"record":{"run_id":"run-card", "repo":dir.path(), "base_ref":"refs/heads/main",
                "tool_path":"/nonexistent"}, "record_path":record_path, "card":card, "prompt":"brief"})
            .to_string(),
        )
        .unwrap();
        execute(vec![request.to_string_lossy().into_owned()]).unwrap();
        let record: Value = serde_json::from_slice(&fs::read(&record_path).unwrap()).unwrap();
        assert_eq!(record["exit_code"], 5, "{record}");
        assert_eq!(record["status"], "failed");
        assert!(
            record["reason"].as_str().unwrap().starts_with(reason),
            "{record}"
        );
        assert!(!dir.path().join("state/runs/run-card/native.log").exists());
    }
}
#[cfg(unix)]
#[test]
fn a_failing_command_carries_its_stdout_and_stderr() {
    let error = output(Command::new("sh").args([
        "-c",
        "echo 'stdout line: gate output'; echo 'stderr line' >&2; exit 3",
    ]))
    .unwrap_err();
    assert!(error.contains("exit status: 3"), "{error}");
    assert!(
        error.contains("stdout: stdout line: gate output"),
        "{error}"
    );
    assert!(error.contains("stderr: stderr line"), "{error}");
    // A tool that prints nothing still names the exit status.
    let error = output(&mut Command::new("false")).unwrap_err();
    assert!(
        error.starts_with("command failed (exit status: 1)"),
        "{error}"
    );
}

#[test]
fn session_excerpt_is_per_head_and_absent_without_a_session_log() {
    let dir = tempfile::tempdir().unwrap();
    let record_dir = dir.path().join("runs/run-x");
    fs::create_dir_all(&record_dir).unwrap();
    let host = Host {
        repo: dir.path().to_path_buf(),
        run_id: "run-x".into(),
        cwd: dir.path().to_path_buf(),
        reviews: dir.path().join("reviews"),
        record_path: record_dir.join("record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    let stamp = dir.path().join("fast-checks").join("h1");
    fs::create_dir_all(stamp.parent().unwrap()).unwrap();
    // No session log: no excerpt, so the brief names none.
    host.session_excerpt(&stamp).unwrap();
    assert!(!stamp.with_extension("session.log").exists());
    // Each stamped head gets only the turns written since the previous one.
    fs::write(record_dir.join("native.log"), "turn one\n").unwrap();
    host.session_excerpt(&stamp).unwrap();
    fs::write(record_dir.join("native.log"), "turn one\nturn two\n").unwrap();
    let second = dir.path().join("fast-checks").join("h2");
    host.session_excerpt(&second).unwrap();
    assert_eq!(
        fs::read_to_string(stamp.with_extension("session.log")).unwrap(),
        "turn one\n"
    );
    assert_eq!(
        fs::read_to_string(second.with_extension("session.log")).unwrap(),
        "turn two\n"
    );
}

#[cfg(unix)]
#[test]
fn session_excerpt_of_a_fixed_head_omits_the_output_of_the_head_whose_gate_failed() {
    const CHILD: &str = "PG_NATIVE_GATE_UMASK_FIXTURE";
    if std::env::var_os(CHILD).is_none() {
        // The mask is process-wide. Set it in a disposable test process so
        // parallel tests retain their own inherited mask.
        let test = std::thread::current();
        let result = output(
            Command::new("sh")
                .args(["-c", "umask 002; exec \"$@\"", "native-gate-umask"])
                .arg(std::env::current_exe().unwrap())
                .args(["--exact", test.name().unwrap(), "--nocapture"])
                .env(CHILD, "1"),
        )
        .unwrap();
        assert!(result.contains("1 passed"), "{result}");
        return;
    }

    let dir = tempfile::tempdir().unwrap();
    let cwd = dir.path().join("worktree");
    fs::create_dir_all(cwd.join("tools")).unwrap();
    let git = |args: &[&str]| {
        output(
            Command::new("git")
                .arg("-C")
                .arg(&cwd)
                .args(args)
                .env("GIT_AUTHOR_NAME", "fixture")
                .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
                .env("GIT_COMMITTER_NAME", "fixture")
                .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
        )
        .unwrap()
    };
    git(&["init", "-q", "-b", "main"]);
    fs::write(
        cwd.join("tools/ci_commit_gate.py"),
        "import os, pathlib, sys\nprint('gate umask:', oct(os.umask(0o022)))\nsys.exit(1 if pathlib.Path('../gate-red').exists() else 0)\n",
    )
    .unwrap();
    git(&["add", "."]);
    git(&["commit", "-q", "-m", "one"]);
    let run = dir.path().join("runs/run-x");
    fs::create_dir_all(&run).unwrap();
    let mut host = Host {
        repo: cwd.clone(),
        run_id: "run-x".into(),
        cwd: cwd.clone(),
        reviews: dir.path().join("reviews"),
        record_path: run.join("record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    git(&["update-ref", &host.base_pin(), "HEAD"]);
    // Head B: cargo output in the session, then a red gate.
    fs::write(run.join("native.log"), "cargo B failed\n").unwrap();
    fs::write(dir.path().join("gate-red"), "").unwrap();
    git(&["commit", "-q", "--allow-empty", "-m", "B"]);
    let head_b = git(&["rev-parse", "HEAD"]);
    assert!(host.fast_checks(&head_b).is_err());
    // Head C fixes it: only its own turns are its excerpt.
    fs::write(run.join("native.log"), "cargo B failed\ncargo C passed\n").unwrap();
    fs::remove_file(dir.path().join("gate-red")).unwrap();
    git(&["commit", "-q", "--allow-empty", "-m", "C"]);
    let head_c = git(&["rev-parse", "HEAD"]);
    host.fast_checks(&head_c).unwrap();
    let gate_log = host.stamp(&head_c).unwrap().with_extension("log");
    assert!(fs::read_to_string(gate_log)
        .unwrap()
        .contains("gate umask: 0o22\n"));
    let excerpt = host.stamp(&head_c).unwrap().with_extension("session.log");
    assert_eq!(fs::read_to_string(excerpt).unwrap(), "cargo C passed\n");
}

#[cfg(unix)]
#[path = "native_writer_late_review_fixture.rs"]
mod late_fixture;
#[cfg(unix)]
use late_fixture::*;

#[cfg(target_os = "linux")]
#[path = "native_writer_policy_tests.rs"]
mod policy_gate;
