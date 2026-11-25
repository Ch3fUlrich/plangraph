use super::*;
use serde_json::json;

#[test]
fn three_rounds_without_a_new_best_stop_and_progress_goes_on() {
    // 3 rounds in a row no lower than the best so far (round 1 has nothing to beat).
    assert_eq!(stop_reason(&[5, 5, 5], 4, false), Some("no_progress"));
    assert_eq!(stop_reason(&[3, 3, 4], 4, false), Some("no_progress"));
    assert_eq!(stop_reason(&[6, 4, 3, 2], 5, false), None);
    assert_eq!(stop_reason(&[4, 2, 1], 4, false), None);
    // One miss or two do not stop a loop that still lowers the count.
    assert_eq!(stop_reason(&[2, 1, 2, 1], 5, false), None);
    assert_eq!(stop_reason(&[6, 4, 4, 3], 5, false), None);
    assert_eq!(stop_reason(&[6, 3, 3, 3], 5, false), None);
    assert_eq!(stop_reason(&[6, 3, 3, 3, 3], 6, false), Some("no_progress"));
    // After round 3 the count must be at most half of round 1.
    assert_eq!(stop_reason(&[8, 7, 6], 4, false), Some("slow_progress"));
    assert_eq!(stop_reason(&[8, 5, 4], 4, false), None);
    assert_eq!(stop_reason(&[8, 6], 3, false), None);
    // The ceiling and the single narrow exception.
    assert_eq!(stop_reason(&[16, 8, 7, 6, 5, 4, 3, 2], 8, false), None);
    assert_eq!(
        stop_reason(&[16, 8, 7, 6, 5, 4, 3, 2], 9, false),
        Some("round_cap")
    );
    assert_eq!(stop_reason(&[4, 2, 1], 4, true), Some("round_cap"));
}

#[test]
fn counts_only_confirmed_blockers_and_preserves_text() {
    let verdict = json!({"writer_run_id":"run-one", "item_key":"p:I", "pr":null,
        "verdict":"REQUEST_CHANGES", "findings":[
        {"severity":"HIGH", "confirmed":false, "text":"disputed"},
        {"severity":"LOW", "confirmed":true, "text":"low"},
        {"severity":"MED", "confirmed":true, "clear_fix":true, "text":"verbatim\nfix"}]});
    let summary = summarize(&verdict, "run-one", "p:I", &Value::Null)
        .ok()
        .unwrap();
    assert_eq!(summary["confirmed_blockers"], 1);
    assert_eq!(summary["narrow_fix_eligible"], true);
    assert_eq!(summary["verdict"]["findings"][2]["text"], "verbatim\nfix");
    assert!(summarize(&verdict, "run-other", "p:I", &Value::Null).is_err());
    let mut bad = verdict.clone();
    bad["findings"][0]["confirmed"] = json!("yes");
    assert!(summarize(&bad, "run-one", "p:I", &Value::Null).is_err());
    bad = verdict.clone();
    bad["verdict"] = json!("ACCEPT");
    assert!(summarize(&bad, "run-one", "p:I", &Value::Null).is_err());
}

fn history(dir: &Path, counts: &[u64], med_only: bool) -> Args {
    for (index, count) in counts.iter().enumerate() {
        let id = format!("run-r{}", index + 1);
        let source = dir.join(format!("verdict-{}.json", index + 1));
        let findings: Vec<_> = (0..*count)
            .map(|_| {
                json!({
            "severity":if med_only { "MED" } else { "HIGH" },
            "confirmed":true, "clear_fix":true, "text":"keep verbatim"})
            })
            .collect();
        std::fs::write(
            &source,
            json!({"writer_run_id":id, "item_key":"p:I", "pr":"https://forge/p/1",
            "verdict":"REQUEST_CHANGES", "findings":findings})
            .to_string(),
        )
        .unwrap();
        crate::runs::create(
            dir,
            &id,
            "fixture",
            json!({"role":"implement", "item_key":"p:I",
            "round_pr":"https://forge/p/1", "writer_round":index+1, "round_verdict_file":source}),
            pg_worker_core::run::record::Status::Completed,
        )
        .ok()
        .unwrap();
    }
    json!({"card":{"item_key":"p:I", "pr":"https://forge/p/1",
        "round_verdict_file":dir.join("next.json")}})
    .as_object()
    .unwrap()
    .clone()
}
fn reason(result: Result<Admission, ToolError>) -> String {
    match result {
        Ok(_) => panic!("expected refusal"),
        Err(e) => e.into_error_data().message.to_string(),
    }
}
#[test]
fn persisted_history_survives_reload_and_refuses_by_reason() {
    for (counts, expected) in [
        (vec![3, 3, 4], Some("round_replan_pending")),
        (vec![4, 2, 1], None),
        (vec![5, 5, 5], Some("round_replan_pending")),
        (vec![8, 7, 6], Some("round_replan_pending")),
        (vec![16, 8, 7, 6, 5, 4, 3, 2], Some("round_cap")),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let args = history(dir.path(), &counts, false);
        if let Some(expected) = expected {
            assert!(reason(admit(dir.path(), &args)).starts_with(expected));
        } else {
            let admitted = admit(dir.path(), &args).ok().unwrap();
            assert_eq!(admitted.fields["writer_round"], counts.len() as u64 + 1);
            assert_eq!(
                admitted.fields["previous_confirmed_blockers"],
                json!(counts)
            );
            // An overlapping caller cannot race this booking.
            assert!(reason(admit(dir.path(), &args)).starts_with("round_admission_busy"));
            drop(admitted);
            // Changing the original verdict cannot erase the observed history.
            std::fs::write(dir.path().join("verdict-1.json"), "{}").unwrap();
            assert!(admit(dir.path(), &args).is_ok());
        }
    }
}
#[test]
fn narrow_med_exception_is_explicit_and_single_use() {
    for count in [1, 2, 3] {
        let dir = tempfile::tempdir().unwrap();
        let mut args = history(dir.path(), &[count, count, count], true);
        assert!(reason(admit(dir.path(), &args)).starts_with("round_replan_pending"));
        args["card"]["narrow_fix"] = json!(true);
        if count == 3 {
            assert!(reason(admit(dir.path(), &args)).starts_with("round_exception_invalid"));
            continue;
        }
        let admission = admit(dir.path(), &args).ok().unwrap();
        let mut fields = admission.fields.clone();
        let source = dir.path().join("next.json");
        std::fs::write(
            &source,
            json!({"writer_run_id":"run-exception", "item_key":"p:I",
            "pr":"https://forge/p/1", "verdict":"REQUEST_CHANGES", "findings":[
                {"severity":"MED", "confirmed":true, "clear_fix":true, "text":"remaining"}]})
            .to_string(),
        )
        .unwrap();
        fields["role"] = json!("implement");
        fields["item_key"] = json!("p:I");
        crate::runs::create(
            dir.path(),
            "run-exception",
            "fixture",
            fields,
            pg_worker_core::run::record::Status::Completed,
        )
        .ok()
        .unwrap();
        drop(admission);
        assert!(reason(admit(dir.path(), &args)).starts_with("round_cap"));
        assert!(dir
            .path()
            .join("runs/run-exception/round-verdict.json")
            .is_file());
    }
    let dir = tempfile::tempdir().unwrap();
    let mut args = history(dir.path(), &[2, 2, 2], false);
    args["card"]["narrow_fix"] = json!(true);
    assert!(reason(admit(dir.path(), &args)).starts_with("round_exception_invalid"));
}
#[test]
fn missing_corrupt_or_mismatched_verdicts_fail_closed() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = history(dir.path(), &[3], false);
    let source = dir.path().join("verdict-1.json");
    std::fs::remove_file(&source).unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_verdict_invalid"));
    std::fs::write(&source, "{}").unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_verdict_invalid"));
    args["card"]["pr"] = json!("https://forge/p/2");
    assert!(reason(admit(dir.path(), &args)).starts_with("round_history_invalid"));
    args.insert("role".into(), json!("review"));
    assert!(admit(dir.path(), &args).is_ok());
}

#[test]
fn ceiling_refuses_even_with_the_narrow_exception_and_acceptance_ends_the_loop() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = history(dir.path(), &[16, 8, 7, 6, 5, 4, 3, 2], true);
    assert!(reason(admit(dir.path(), &args)).starts_with("round_cap"));
    args["card"]["narrow_fix"] = json!(true);
    assert!(reason(admit(dir.path(), &args)).starts_with("round_cap"));
    let dir = tempfile::tempdir().unwrap();
    let mut args = history(dir.path(), &[5, 5, 5], true);
    assert!(reason(admit(dir.path(), &args)).starts_with("round_replan_pending"));
    args["card"]["narrow_fix"] = json!(true);
    assert!(admit(dir.path(), &args).is_err());
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[2, 1, 1], true);
    assert_eq!(
        admit(dir.path(), &args).ok().unwrap().fields["writer_round"],
        4
    );
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[0], false);
    let source = dir.path().join("verdict-1.json");
    let mut verdict: Value = serde_json::from_slice(&std::fs::read(&source).unwrap()).unwrap();
    verdict["verdict"] = json!("ACCEPT");
    std::fs::write(source, verdict.to_string()).unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_accepted"));
}

#[test]
fn final_narrow_round_is_not_a_stop_while_running_or_after_acceptance() {
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[2, 2, 2, 0], true);
    let path = dir.path().join("runs/run-r4/record.json");
    let mut record: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    record["narrow_fix"] = json!(true);
    record["status"] = json!("running");
    std::fs::write(&path, record.to_string()).unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_in_flight"));
    record["status"] = json!("completed");
    std::fs::write(path, record.to_string()).unwrap();
    let source = dir.path().join("verdict-4.json");
    let mut verdict: Value = serde_json::from_slice(&std::fs::read(&source).unwrap()).unwrap();
    verdict["verdict"] = json!("ACCEPT");
    std::fs::write(source, verdict.to_string()).unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_accepted"));
    assert!(dir.path().join("runs/run-r4/round-verdict.json").is_file());
}

#[test]
fn unrelated_items_do_not_contend_for_admission() {
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[], false);
    let first = admit(dir.path(), &args).ok().unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_admission_busy"));
    let mut other = args.clone();
    other["card"]["item_key"] = json!("p:OTHER");
    assert!(admit(dir.path(), &other).is_ok());
    drop(first);
}
#[test]
fn prompt_only_run_directory_does_not_block_writers() {
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[], false);
    let orphan = dir.path().join("runs/run-orphan");
    std::fs::create_dir_all(&orphan).unwrap();
    std::fs::write(orphan.join("prompt.txt"), "launch never started").unwrap();
    assert!(admit(dir.path(), &args).is_ok());
    // An existing but malformed record remains a refusal, not silent lost history.
    std::fs::write(orphan.join("record.json"), "{").unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_history_unavailable"));
}

#[test]
fn failed_round_without_work_does_not_consume_round_or_add_zero_baseline() {
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[3, 2], false);
    crate::runs::create(
        dir.path(),
        "run-void",
        "fixture",
        json!({"role":"implement", "item_key":"p:I", "writer_round":3,
            "round_void":true, "pid":42}),
        pg_worker_core::run::record::Status::Failed,
    )
    .ok()
    .unwrap();
    let admission = admit(dir.path(), &args).ok().unwrap();
    assert_eq!(admission.fields["writer_round"], 3);
    assert_eq!(
        admission.fields["previous_confirmed_blockers"],
        json!([3, 2])
    );
}
#[test]
fn work_fingerprint_detects_commits_tracked_and_untracked_changes() {
    let dir = tempfile::tempdir().unwrap();
    let git = |args: &[&str]| {
        let output = pg_worker_core::git_test_support::command(dir.path())
            .args(args)
            .envs([
                ("GIT_AUTHOR_NAME", "Fixture"),
                ("GIT_AUTHOR_EMAIL", "fixture@example.test"),
                ("GIT_COMMITTER_NAME", "Fixture"),
                ("GIT_COMMITTER_EMAIL", "fixture@example.test"),
            ])
            .current_dir(dir.path())
            .output()
            .unwrap();
        assert!(output.status.success());
    };
    git(&["init", "-q"]);
    git(&["commit", "-qm", "initial", "--allow-empty"]);
    let initial = work_fingerprint(dir.path()).unwrap();
    std::fs::write(dir.path().join("work"), "one").unwrap();
    assert_ne!(work_fingerprint(dir.path()).unwrap(), initial);
    git(&["add", "work"]);
    git(&["commit", "-qm", "work"]);
    let committed = work_fingerprint(dir.path()).unwrap();
    std::fs::write(dir.path().join("work"), "two").unwrap();
    assert_ne!(work_fingerprint(dir.path()).unwrap(), committed);
    let state = tempfile::tempdir().unwrap();
    let mut record = crate::runs::create(
        state.path(),
        "run-failed",
        "fixture",
        json!({"writer_round":1, "pid":42, "cwd":dir.path(),
            "round_work_before":work_fingerprint(dir.path())}),
        pg_worker_core::run::record::Status::Running,
    )
    .ok()
    .unwrap();
    finish_round(&mut record, pg_worker_core::run::record::Status::Failed);
    assert_eq!(record.get("round_void"), Some(&json!(true)));
    std::fs::write(dir.path().join("work"), "three").unwrap();
    finish_round(&mut record, pg_worker_core::run::record::Status::Failed);
    assert_eq!(record.get("round_void"), Some(&json!(false)));
    record.set("pid", Value::Null);
    finish_round(&mut record, pg_worker_core::run::record::Status::Failed);
    assert_eq!(record.get("round_void"), Some(&json!(true)));
}

#[test]
fn the_places_that_quote_the_stop_rule_name_no_other_round_number() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let text = |path: &str| std::fs::read_to_string(root.join(path)).unwrap();
    let batch = text("skills/unattended-orchestration/cards/batch.md");
    let card = batch
        .split("R-batch-04")
        .nth(1)
        .unwrap()
        .split("R-batch-05")
        .next()
        .unwrap()
        .to_string();
    let skill = text("skills/batch-writer/SKILL.md");
    let step = skill
        .split("10. On REQUEST_CHANGES")
        .nth(1)
        .unwrap()
        .split("## Never")
        .next()
        .unwrap()
        .to_string();
    let settings = text("src/plangraph/settings.py");
    let setting = settings
        .lines()
        .find(|l| l.contains("max_rounds: int"))
        .unwrap()
        .to_string();
    let allowed = [1, STALL_ROUNDS, MAX_ROUNDS, MAX_ROUNDS + 1];
    let number = regex_lite_numbers;
    for (name, body) in [
        ("R-batch-04", card),
        ("batch-writer step 10", step),
        ("settings.py", setting),
    ] {
        for n in number(&body) {
            assert!(allowed.contains(&n), "{name} names the round number {n}");
        }
        assert!(
            name == "settings.py" || body.contains(&MAX_ROUNDS.to_string()),
            "{name} quotes the ceiling"
        );
    }
    assert!(settings.contains(&format!("max_rounds: int = {MAX_ROUNDS}")));
}

/// Numbers that follow "round"/"rounds"/"ROUND_CAP (at most"/"max_rounds: int =" in `body`.
fn regex_lite_numbers(body: &str) -> Vec<u64> {
    let mut found = vec![];
    for key in [
        "round ", "rounds ", "rounds\n", "at most ", "int = ", "Round ", "round\n",
    ] {
        for part in body.split(key).skip(1) {
            let digits: String = part
                .trim_start()
                .chars()
                .take_while(|c| c.is_ascii_digit())
                .collect();
            if let Ok(n) = digits.parse() {
                found.push(n);
            }
        }
    }
    found
}
