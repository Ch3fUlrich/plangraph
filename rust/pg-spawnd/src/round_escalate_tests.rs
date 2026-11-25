use super::*;
use crate::{
    placement::Args,
    round_progress::{admit, Admission},
};
use pg_worker_core::run::record::Status;
use serde_json::json;

const PR: &str = "https://forge/p/1";

/// An earlier writer round of batch `p:I` (extra fields merged over the defaults).
fn book(dir: &Path, id: &str, round: u64, extra: Value) {
    let mut record = json!({"role":"implement", "item_key":"p:I", "round_pr":PR,
        "writer_round":round, "round_verdict_file":null, "round_cause":"red_ci",
        "round_failing_checks":5});
    record
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    crate::runs::create(dir, id, "fixture", record, Status::Completed)
        .ok()
        .unwrap();
}
fn reds(dir: &Path, checks: &[u64]) {
    for (i, c) in checks.iter().enumerate() {
        book(
            dir,
            &format!("run-r{}", i + 1),
            i as u64 + 1,
            json!({"round_failing_checks":c}),
        );
    }
}
fn card(cause: &str, checks: u64) -> Args {
    json!({"card":{"item_key":"p:I", "pr":PR, "round_cause":cause, "failing_checks":checks}})
        .as_object()
        .unwrap()
        .clone()
}
fn message(result: Result<Admission, ToolError>) -> String {
    result
        .err()
        .expect("a refusal")
        .into_error_data()
        .message
        .to_string()
}
fn jobs(dir: &Path) -> Vec<Value> {
    let read = std::fs::read_dir(dir.join("replans"));
    read.into_iter()
        .flatten()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .map(|p| serde_json::from_slice(&std::fs::read(p).unwrap()).unwrap())
        .collect()
}
/// Mark every queued job as planned and reported, as the runner leaves it.
fn finish_jobs(dir: &Path) {
    for entry in std::fs::read_dir(dir.join("replans")).unwrap() {
        let path = entry.unwrap().path();
        let mut job: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        job["plan"] = json!({"leg":"strong", "findings":["HIGH a.rs:1 keep"],
            "slices":[{"key":"p:I-S1", "files":["a/one.rs"]}]});
        job["report"] = json!("sent");
        std::fs::write(path, job.to_string()).unwrap();
    }
}

#[test]
fn the_saved_replan_job_governs_admission_through_reporting_and_resume() {
    let dir = tempfile::tempdir().unwrap();
    reds(dir.path(), &[5, 5, 5]);
    assert!(message(admit(dir.path(), &card("red_ci", 5))).starts_with("round_replan_pending"));
    assert!(message(admit(dir.path(), &card("red_ci", 2))).starts_with("round_replan_pending"));
    assert_eq!(jobs(dir.path()).len(), 1);
    finish_jobs(dir.path());
    let admitted = admit(dir.path(), &card("red_ci", 2)).ok().unwrap();
    assert_eq!(
        (
            admitted.fields["writer_round"].clone(),
            admitted.fields["round_escalation"].clone()
        ),
        (json!(4), json!(1))
    );
    let brief = admitted.prompt_addendum.clone().unwrap();
    assert!(brief.contains("p:I-S1: a/one.rs") && brief.contains("Open finding: HIGH a.rs:1 keep"));
}

#[test]
fn falling_failures_queue_no_job_and_a_dry_run_never_queues_one() {
    let dir = tempfile::tempdir().unwrap();
    reds(dir.path(), &[9, 7, 5]);
    let kept = admit(dir.path(), &card("red_ci", 3)).ok().unwrap();
    assert_eq!(kept.fields["previous_confirmed_blockers"], json!([7, 5, 3]));
    assert!(jobs(dir.path()).is_empty());
    drop(kept);
    let dir = tempfile::tempdir().unwrap();
    reds(dir.path(), &[5, 5, 5]);
    let mut dry = card("red_ci", 5);
    dry.insert("dry_run".into(), json!(true));
    assert!(message(admit(dir.path(), &dry)).starts_with("round_replan_pending"));
    assert!(jobs(dir.path()).is_empty());
}

#[test]
fn a_first_launch_without_a_cause_is_the_initial_implementation_and_holds_its_batch() {
    let dir = tempfile::tempdir().unwrap();
    let launch = json!({"card":{"item_key":"p:I", "pr":PR}})
        .as_object()
        .unwrap()
        .clone();
    let admitted = admit(dir.path(), &launch).ok().unwrap();
    assert!(admitted.fields.get("writer_round").is_none());
    let mut fields = admitted.fields.clone();
    drop(admitted);
    fields["role"] = json!("implement");
    fields["item_key"] = json!("p:I");
    crate::runs::create(dir.path(), "run-init", "fixture", fields, Status::Running)
        .ok()
        .unwrap();
    assert!(message(admit(dir.path(), &launch)).starts_with("round_in_flight"));
    assert!(message(admit(dir.path(), &card("red_ci", 2))).starts_with("round_in_flight"));
    let path = dir.path().join("runs/run-init/record.json");
    let mut record: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    record["status"] = json!("completed");
    std::fs::write(path, record.to_string()).unwrap();
    // The same card, still without a cause, is now the first fix (review default): round 1.
    let first = admit(dir.path(), &launch).ok().unwrap();
    assert_eq!(first.fields["writer_round"], 1);
    assert_eq!(first.fields["round_cause"], "review_finding");
    drop(first);
}

#[test]
fn one_batch_key_shares_one_ledger_across_item_keys() {
    let dir = tempfile::tempdir().unwrap();
    for (i, item) in ["p:One", "p:Two", "p:One"].into_iter().enumerate() {
        book(
            dir.path(),
            &format!("run-r{}", i + 1),
            i as u64 + 1,
            json!({"item_key":item, "batch_key":"p:Batch"}),
        );
    }
    let mut args = card("red_ci", 5);
    args["card"]["item_key"] = json!("p:Three");
    args["card"]["batch_key"] = json!("p:Batch");
    assert!(message(admit(dir.path(), &args)).starts_with("round_replan_pending"));
    assert_eq!(jobs(dir.path())[0]["item"], "p:Batch");
}

/// A numbered round with a verdict file (no findings) naming the round's own item and PR.
fn reviewed(dir: &Path, id: &str, round: u64, verdict: &str, extra: Value) {
    let (item, pr) = (
        extra["item_key"].as_str().unwrap_or("p:I"),
        extra.get("round_pr").cloned().unwrap_or(json!(PR)),
    );
    let source = dir.join(format!("{id}.json"));
    let body =
        json!({"writer_run_id":id, "item_key":item, "pr":pr, "verdict":verdict, "findings":[]});
    std::fs::write(&source, body.to_string()).unwrap();
    let mut merged = json!({"round_verdict_file":source, "round_cause":"review_finding"});
    merged
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    book(dir, id, round, merged);
}
fn without_checks(mut args: Args) -> Args {
    args["card"]
        .as_object_mut()
        .unwrap()
        .remove("failing_checks");
    args
}

#[test]
fn check_rounds_need_evidence_and_an_accepted_review_allows_a_check_fix() {
    let dir = tempfile::tempdir().unwrap();
    reds(dir.path(), &[1]);
    for cause in ["red_ci", "rebase_main", "fast_check"] {
        let args = without_checks(card(cause, 0));
        assert!(message(admit(dir.path(), &args)).starts_with("round_failing_checks_missing"));
    }
    let dir = tempfile::tempdir().unwrap();
    reviewed(dir.path(), "run-r1", 1, "ACCEPT", json!({}));
    assert!(message(admit(dir.path(), &card("review_finding", 0))).starts_with("round_accepted"));
    assert!(admit(dir.path(), &card("fast_check", 2)).is_ok());
}

#[test]
fn unknown_check_counts_never_credit_progress() {
    // A red round, then two review rounds with no check count: the last observed count carries
    // over both omissions and the stall is not reset to zero.
    let dir = tempfile::tempdir().unwrap();
    reds(dir.path(), &[5]);
    for index in 2..=3 {
        let id = format!("run-r{index}");
        reviewed(
            dir.path(),
            &id,
            index,
            "REQUEST_CHANGES",
            json!({"round_failing_checks":null}),
        );
    }
    let args = without_checks(card("review_finding", 0));
    assert!(message(admit(dir.path(), &args)).starts_with("round_replan_pending"));
}

#[test]
fn members_and_pre_pr_rounds_keep_their_own_verdict_identity() {
    let dir = tempfile::tempdir().unwrap();
    // Round 1 (member One) ran before the batch had a PR; round 2 (member Two) has the first PR.
    reviewed(
        dir.path(),
        "run-r1",
        1,
        "REQUEST_CHANGES",
        json!({"item_key":"p:One", "batch_key":"p:Batch", "round_pr":null}),
    );
    reviewed(
        dir.path(),
        "run-r2",
        2,
        "REQUEST_CHANGES",
        json!({"item_key":"p:Two", "batch_key":"p:Batch"}),
    );
    let mut args = card("red_ci", 2);
    args["card"]["item_key"] = json!("p:Three");
    args["card"]["batch_key"] = json!("p:Batch");
    assert_eq!(
        admit(dir.path(), &args).ok().unwrap().fields["writer_round"],
        3
    );
}
