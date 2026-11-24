use super::*;
use crate::{
    placement::Args,
    round_progress::{admit, Admission},
};
use serde_json::json;
use std::path::Path;

const PR: &str = "https://forge/p/1";

/// One earlier writer round: why it ran and the failing checks seen when it started.
struct Round {
    cause: &'static str,
    checks: Option<u64>,
    escalation: u64,
}
fn red(checks: u64) -> Round {
    Round {
        cause: "red_ci",
        checks: Some(checks),
        escalation: 0,
    }
}
fn escalated(checks: u64, level: u64) -> Round {
    Round {
        escalation: level,
        ..red(checks)
    }
}

fn book(dir: &Path, rounds: &[Round]) {
    for (index, round) in rounds.iter().enumerate() {
        crate::runs::create(
            dir,
            &format!("run-r{}", index + 1),
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "round_pr":PR,
            "writer_round":index + 1, "round_verdict_file":null, "round_cause":round.cause,
            "round_failing_checks":round.checks, "round_escalation":round.escalation,
            "started":1_000_000.0 + index as f64}),
            pg_worker_core::run::record::Status::Completed,
        )
        .ok()
        .unwrap();
    }
}
fn card(cause: &str, checks: u64) -> Args {
    json!({"card":{"item_key":"p:I", "pr":PR, "round_cause":cause, "failing_checks":checks}})
        .as_object()
        .unwrap()
        .clone()
}
fn run(dir: &Path, args: &Args) -> Result<Admission, ToolError> {
    admit(dir, args)
}
fn message(result: Result<Admission, ToolError>) -> String {
    match result {
        Ok(_) => panic!("expected a refusal"),
        Err(e) => e.into_error_data().message.to_string(),
    }
}
fn replans(dir: &Path) -> Vec<Value> {
    let mut jobs: Vec<Value> = std::fs::read_dir(dir.join("replans"))
        .into_iter()
        .flatten()
        .map(|e| serde_json::from_slice(&std::fs::read(e.unwrap().path()).unwrap()).unwrap())
        .collect();
    jobs.sort_by_key(|j| j["level"].as_u64());
    jobs
}

/// Mark every queued replan job as planned and reported, as the replan runner leaves it.
fn finish_replans(dir: &Path) {
    for entry in std::fs::read_dir(dir.join("replans")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let mut job: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        job["plan"] = json!({"leg":"strong", "slices":[]});
        job["report"] = json!("sent");
        std::fs::write(path, job.to_string()).unwrap();
    }
}

#[test]
fn three_red_ci_rounds_without_fewer_failures_wait_for_a_replan_then_keep_the_writer() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5), red(5), red(5)]);
    // The stalled round is refused, uses no round, and the replan job is queued once.
    for _ in 0..2 {
        assert!(message(run(dir.path(), &card("red_ci", 5))).starts_with("round_replan_pending"));
    }
    let jobs = replans(dir.path());
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0]["reason"], "escalated");
    assert_eq!(jobs[0]["level"], 1);
    assert_eq!(jobs[0]["pr"], PR);
    finish_replans(dir.path());
    let admitted = run(dir.path(), &card("red_ci", 5)).ok().unwrap();
    assert_eq!(admitted.fields["writer_round"], 4);
    assert_eq!(
        admitted.fields["previous_confirmed_blockers"],
        json!([5, 5, 5])
    );
    assert_eq!(admitted.fields["round_escalation"], 1);
    assert_eq!(admitted.fields["round_escalation_reason"], "no_progress");
    assert_eq!(admitted.fields["round_cause"], "red_ci");
    assert_eq!(admitted.fields["round_failing_checks"], 5);
    assert_eq!(admitted.fields["round_replan"], "done");
    // The writer is never switched: the admission names no leg.
    assert!(admitted.fields.get("round_leg").is_none());
}

#[test]
fn a_stall_without_a_pr_goes_back_instead_of_advancing_the_level() {
    let mut args = card("red_ci", 5);
    args["card"].as_object_mut().unwrap().remove("pr");
    let dir2 = tempfile::tempdir().unwrap();
    for index in 1..=3 {
        crate::runs::create(
            dir2.path(),
            &format!("run-r{index}"),
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "writer_round":index,
            "round_verdict_file":null, "round_cause":"red_ci", "round_failing_checks":5}),
            pg_worker_core::run::record::Status::Completed,
        )
        .ok()
        .unwrap();
    }
    assert!(message(run(dir2.path(), &args)).starts_with("no_progress"));
    assert!(replans(dir2.path()).is_empty());
}

#[test]
fn a_batch_whose_failures_fall_keeps_its_writer() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(9), red(7), red(5)]);
    let admitted = run(dir.path(), &card("red_ci", 3)).ok().unwrap();
    assert_eq!(
        admitted.fields["previous_confirmed_blockers"],
        json!([7, 5, 3])
    );
    assert_eq!(admitted.fields["round_escalation"], 0);
    assert_eq!(admitted.fields["round_replan"], Value::Null);
    assert!(replans(dir.path()).is_empty());
}

#[test]
fn a_check_judged_round_followed_by_a_card_without_failing_checks_is_admitted() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5)]);
    let mut args = card("review_finding", 0);
    args["card"]
        .as_object_mut()
        .unwrap()
        .remove("failing_checks");
    let admitted = run(dir.path(), &args).ok().unwrap();
    assert_eq!(admitted.fields["writer_round"], 2);
    // The unknown check count after the round keeps the count it started with: no credit.
    assert_eq!(admitted.fields["previous_confirmed_blockers"], json!([5]));
}

#[test]
fn consecutive_cards_without_a_check_count_carry_the_last_observed_count() {
    let dir = tempfile::tempdir().unwrap();
    let unknown = || Round {
        checks: None,
        ..red(0)
    };
    book(dir.path(), &[red(5), unknown(), unknown()]);
    let mut args = card("review_finding", 0);
    args["card"]
        .as_object_mut()
        .unwrap()
        .remove("failing_checks");
    // 5, then two omissions: the known red count stays, the stall is not reset to zero.
    assert!(message(run(dir.path(), &args)).starts_with("round_replan_pending"));
}

#[test]
fn a_review_card_without_a_check_count_cannot_reset_a_stall() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5), red(5), red(5)]);
    let mut args = card("review_finding", 0);
    args["card"]
        .as_object_mut()
        .unwrap()
        .remove("failing_checks");
    assert!(message(run(dir.path(), &args)).starts_with("round_replan_pending"));
}

#[test]
fn a_review_round_without_a_verdict_is_refused_even_when_the_next_card_reports_zero_checks() {
    let dir = tempfile::tempdir().unwrap();
    book(
        dir.path(),
        &[Round {
            cause: "review_finding",
            ..red(0)
        }],
    );
    assert!(
        message(run(dir.path(), &card("review_finding", 0))).starts_with("round_verdict_missing")
    );
}

#[test]
fn a_dry_run_admission_queues_no_replan_and_a_dry_run_refusal_closes_no_pr() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5), red(5), red(5)]);
    let mut args = card("red_ci", 5);
    args.insert("dry_run".into(), json!(true));
    assert!(message(run(dir.path(), &args)).starts_with("round_replan_pending"));
    assert!(replans(dir.path()).is_empty());
    // Round nine on a dry run: the refusal is recorded but queues no job that closes the PR.
    let dir = tempfile::tempdir().unwrap();
    let eight: Vec<Round> = (0..8).map(|_| red(5)).collect();
    book(dir.path(), &eight);
    let error = run(dir.path(), &args).err().unwrap();
    let refusal = crate::round_progress::record_refusal(dir.path(), "fixture", &args, error)
        .ok()
        .unwrap();
    assert_eq!(refusal["reason"], "round_cap");
    assert!(refusal.get("replan").is_none());
    assert!(replans(dir.path()).is_empty());
}

#[test]
fn an_accepted_review_still_allows_a_counted_fix_for_red_checks_or_a_moved_main() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("v.json");
    std::fs::write(
        &source,
        json!({"writer_run_id":"run-r1", "item_key":"p:I", "pr":PR,
        "verdict":"ACCEPT", "findings":[]})
        .to_string(),
    )
    .unwrap();
    crate::runs::create(
        dir.path(),
        "run-r1",
        "fixture",
        json!({"role":"implement", "item_key":"p:I", "round_pr":PR, "writer_round":1,
        "round_verdict_file":source, "round_cause":"review_finding"}),
        pg_worker_core::run::record::Status::Completed,
    )
    .ok()
    .unwrap();
    assert!(message(run(dir.path(), &card("review_finding", 0))).starts_with("round_accepted"));
    for cause in ["red_ci", "rebase_main", "fast_check"] {
        let admitted = run(dir.path(), &card(cause, 2)).ok().unwrap();
        assert_eq!(admitted.fields["writer_round"], 2);
        assert_eq!(admitted.fields["previous_confirmed_blockers"], json!([2]));
    }
}

#[test]
fn a_check_triggered_round_must_carry_its_failing_checks() {
    let dir = tempfile::tempdir().unwrap();
    for cause in ["red_ci", "rebase_main", "fast_check"] {
        let mut args = card(cause, 0);
        args["card"]
            .as_object_mut()
            .unwrap()
            .remove("failing_checks");
        assert!(message(run(dir.path(), &args)).starts_with("round_failing_checks_missing"));
    }
}

#[test]
fn the_report_ignores_dry_run_and_voided_attempts() {
    let dir = tempfile::tempdir().unwrap();
    for (id, status, extra) in [
        (
            "run-d",
            pg_worker_core::run::record::Status::DryRun,
            json!({}),
        ),
        (
            "run-v",
            pg_worker_core::run::record::Status::Failed,
            json!({"round_void":true}),
        ),
    ] {
        let mut base = json!({"role":"implement", "item_key":"p:Ghost", "writer_round":1,
            "round_cause":"red_ci", "started":5.0});
        base.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        crate::runs::create(dir.path(), id, "fixture", base, status)
            .ok()
            .unwrap();
    }
    let report = report(
        dir.path(),
        &crate::records::list_runs(dir.path()),
        0.0,
        f64::MAX,
    );
    assert_eq!(report["items"], json!({}));
    assert_eq!(report["escalations_per_day"], json!({}));
}

#[test]
fn returns_are_not_replans_and_a_job_of_the_earlier_schema_is_still_reported() {
    let dir = tempfile::tempdir().unwrap();
    crate::round_replan::enqueue_return(dir.path(), "p:Back", "round_cap", &[])
        .ok()
        .unwrap();
    // An older job file without queued_at is dated by the file.
    std::fs::write(
        dir.path().join("replans/replan-old.json"),
        json!({"item":"p:Old", "pr":PR, "reason":"no_progress", "plan":null,
        "defined":[], "closed":false, "report":"pending", "error":null})
        .to_string(),
    )
    .unwrap();
    let report = report(dir.path(), &[], 0.0, f64::MAX);
    assert_eq!(report["items"]["p:Back"]["returns"], 1);
    assert_eq!(report["items"]["p:Back"]["replans"], 0);
    assert_eq!(report["items"]["p:Old"]["replans"], 1);
}

#[test]
fn a_batch_waiting_for_its_replan_is_reported_by_the_day_the_job_was_queued() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5), red(5), red(5)]);
    assert!(message(run(dir.path(), &card("red_ci", 5))).starts_with("round_replan_pending"));
    let queued = replans(dir.path())[0]["queued_at"].as_f64().unwrap();
    let day = chrono::DateTime::from_timestamp(queued as i64, 0)
        .unwrap()
        .format("%Y-%m-%d")
        .to_string();
    let records = crate::records::list_runs(dir.path());
    let waiting = report(dir.path(), &records, 0.0, f64::MAX);
    assert_eq!(waiting["items"]["p:I"]["rounds"], 3);
    assert_eq!(waiting["items"]["p:I"]["escalations"], 1);
    assert_eq!(waiting["items"]["p:I"]["replans"], 1);
    assert_eq!(waiting["escalations_per_day"], json!({ day.clone(): 1 }));
    // A retry after the job finished adds no event and moves no day.
    finish_replans(dir.path());
    let again = report(dir.path(), &records, 0.0, f64::MAX);
    assert_eq!(again["escalations_per_day"], json!({ day: 1 }));
    // A window before the queueing sees none of it.
    let before = report(dir.path(), &records, 0.0, queued);
    assert_eq!(before["items"]["p:I"]["escalations"], 0);
}

#[test]
fn every_cause_counts_and_an_unknown_cause_or_check_count_is_refused() {
    for cause in CAUSES {
        let dir = tempfile::tempdir().unwrap();
        let admitted = run(dir.path(), &card(cause, 2)).ok().unwrap();
        assert_eq!(admitted.fields["round_cause"], cause);
        assert_eq!(admitted.fields["writer_round"], 1);
    }
    let dir = tempfile::tempdir().unwrap();
    assert!(message(run(dir.path(), &card("vibes", 2))).starts_with("round_cause_invalid"));
    let mut bad = card("red_ci", 2);
    bad["card"]["failing_checks"] = json!("many");
    assert!(message(run(dir.path(), &bad)).starts_with("round_failing_checks_invalid"));
    // No cause on the card is a review finding, the older behaviour.
    let mut plain = card("red_ci", 2);
    plain["card"].as_object_mut().unwrap().remove("round_cause");
    assert_eq!(
        run(dir.path(), &plain).ok().unwrap().fields["round_cause"],
        "review_finding"
    );
}

#[test]
fn each_new_stall_window_queues_one_more_replan_and_the_ceiling_ends_the_loop() {
    // Round 5 follows the replanned round: one miss, no second replan.
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5), red(5), red(5), escalated(5, 1)]);
    let kept = run(dir.path(), &card("red_ci", 5)).ok().unwrap();
    assert_eq!(kept.fields["round_escalation"], 1);
    assert_eq!(kept.fields["round_replan"], Value::Null);
    drop(kept);
    // Three more misses on the new level queue the next replan.
    let dir = tempfile::tempdir().unwrap();
    book(
        dir.path(),
        &[
            red(5),
            red(5),
            red(5),
            escalated(5, 1),
            escalated(5, 1),
            escalated(5, 1),
        ],
    );
    assert!(message(run(dir.path(), &card("red_ci", 5))).starts_with("round_replan_pending"));
    assert_eq!(replans(dir.path())[0]["level"], 2);
    finish_replans(dir.path());
    let again = run(dir.path(), &card("red_ci", 5)).ok().unwrap();
    assert_eq!(again.fields["writer_round"], 7);
    assert_eq!(again.fields["round_escalation"], 2);
    drop(again);
    // Round 9 is refused whatever the history.
    let dir = tempfile::tempdir().unwrap();
    let eight: Vec<Round> = (0..8).map(|_| red(5)).collect();
    book(dir.path(), &eight);
    assert!(message(run(dir.path(), &card("red_ci", 5))).starts_with("round_cap"));
}

#[test]
fn failing_checks_added_to_review_findings_decide_progress() {
    let dir = tempfile::tempdir().unwrap();
    // Findings fall 3, 2, 1 but the red checks that follow each round grow: no progress.
    for (index, (blockers, checks)) in [(3u64, 0u64), (2, 1), (1, 3)].into_iter().enumerate() {
        let id = format!("run-r{}", index + 1);
        let source = dir.path().join(format!("v{index}.json"));
        let findings: Vec<_> = (0..blockers)
            .map(|_| json!({"severity":"HIGH", "confirmed":true, "text":"t"}))
            .collect();
        std::fs::write(
            &source,
            json!({"writer_run_id":id, "item_key":"p:I", "pr":PR,
            "verdict":"REQUEST_CHANGES", "findings":findings})
            .to_string(),
        )
        .unwrap();
        crate::runs::create(
            dir.path(),
            &id,
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "round_pr":PR,
            "writer_round":index + 1, "round_verdict_file":source,
            "round_failing_checks":checks, "round_cause":"review_finding"}),
            pg_worker_core::run::record::Status::Completed,
        )
        .ok()
        .unwrap();
    }
    assert!(message(run(dir.path(), &card("fast_check", 4))).starts_with("round_replan_pending"));
    finish_replans(dir.path());
    let admitted = run(dir.path(), &card("fast_check", 4)).ok().unwrap();
    // 3+1, 2+3, 1+4: 4, 5, 5 never beats 4 after round 1.
    assert_eq!(
        admitted.fields["previous_confirmed_blockers"],
        json!([4, 5, 5])
    );
    assert_eq!(admitted.fields["round_escalation"], 1);
}

#[test]
fn round_nine_is_refused_and_the_batch_goes_back_with_a_replan() {
    let dir = tempfile::tempdir().unwrap();
    let rounds: Vec<Round> = (0..8).map(|n| red(20 - n * 2)).collect();
    book(dir.path(), &rounds);
    let text = message(run(dir.path(), &card("red_ci", 1)));
    assert!(text.starts_with("round_cap"));
    let refusal = crate::round_progress::record_refusal(
        dir.path(),
        "fixture",
        &card("red_ci", 1),
        crate::placement::parameter(text),
    )
    .ok()
    .unwrap();
    assert_eq!(refusal["replan"], "queued");
    assert_eq!(replans(dir.path())[0]["reason"], "round_cap");
}

#[test]
fn the_report_counts_rounds_by_cause_escalations_and_replans() {
    let dir = tempfile::tempdir().unwrap();
    book(dir.path(), &[red(5), red(5), red(5)]);
    assert!(message(run(dir.path(), &card("fast_check", 5))).starts_with("round_replan_pending"));
    finish_replans(dir.path());
    let admitted = run(dir.path(), &card("fast_check", 5)).ok().unwrap();
    let mut fields = admitted.fields.clone();
    drop(admitted);
    fields["role"] = json!("implement");
    fields["item_key"] = json!("p:I");
    fields["started"] = json!(1_000_100.0);
    crate::runs::create(
        dir.path(),
        "run-r4",
        "fixture",
        fields,
        pg_worker_core::run::record::Status::Completed,
    )
    .ok()
    .unwrap();
    let records = crate::records::list_runs(dir.path());
    let report = report(dir.path(), &records, 0.0, f64::MAX);
    let item = &report["items"]["p:I"];
    assert_eq!(item["rounds"], 4);
    assert_eq!(item["by_cause"], json!({"red_ci":3, "fast_check":1}));
    assert_eq!(item["escalations"], 1);
    assert_eq!(item["replans"], 1);
    assert_eq!(report["max_rounds"], 4);
    assert_eq!(report["over_cap"], json!([]));
}
