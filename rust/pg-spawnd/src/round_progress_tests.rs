use super::*;
use serde_json::json;

#[test]
fn strict_best_and_consecutive_misses() {
    assert_eq!(stop_reason(&[3, 3, 4], 4, false), Some("no_progress"));
    assert_eq!(stop_reason(&[4, 2, 1], 4, false), None);
    assert_eq!(stop_reason(&[2, 1, 2, 1], 5, false), Some("no_progress"));
    assert_eq!(stop_reason(&[8, 7, 6, 5], 5, false), Some("round_cap"));
    assert_eq!(stop_reason(&[8, 7, 6, 5, 4], 6, false), Some("round_cap"));
    assert_eq!(stop_reason(&[3, 3, 2, 2], 5, false), Some("round_cap"));
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
        (vec![3, 3, 4], Some("no_progress")),
        (vec![4, 2, 1], None),
        (vec![2, 1, 2, 1], Some("no_progress")),
        (vec![8, 7, 6, 5], Some("round_cap")),
        (vec![8, 7, 6, 5, 4], Some("round_cap")),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let args = history(dir.path(), &counts, false);
        if let Some(expected) = expected {
            assert!(reason(admit(dir.path(), &args)).starts_with(expected));
        } else {
            let admitted = admit(dir.path(), &args).ok().unwrap();
            assert_eq!(admitted.fields["writer_round"], 4);
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
        assert!(reason(admit(dir.path(), &args)).starts_with("no_progress"));
        args["card"]["narrow_fix"] = json!(true);
        if count == 3 {
            assert!(reason(admit(dir.path(), &args)).starts_with("round_exception_invalid"));
            continue;
        }
        let admission = admit(dir.path(), &args).ok().unwrap();
        let mut fields = admission.fields.clone();
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
fn fifth_round_exception_and_acceptance() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = history(dir.path(), &[5, 4, 3, 2], true);
    assert!(reason(admit(dir.path(), &args)).starts_with("round_cap"));
    args["card"]["narrow_fix"] = json!(true);
    let admission = admit(dir.path(), &args).ok().unwrap();
    assert_eq!(admission.fields["writer_round"], 5);
    drop(admission);
    let dir = tempfile::tempdir().unwrap();
    let args = history(dir.path(), &[0], false);
    let source = dir.path().join("verdict-1.json");
    let mut verdict: Value = serde_json::from_slice(&std::fs::read(&source).unwrap()).unwrap();
    verdict["verdict"] = json!("ACCEPT");
    std::fs::write(source, verdict.to_string()).unwrap();
    assert!(reason(admit(dir.path(), &args)).starts_with("round_accepted"));
}
