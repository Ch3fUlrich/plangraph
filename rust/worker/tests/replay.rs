//! Offline workflow replay tests: no Temporal server, no network. The histories
//! in `tests/fixtures` were recorded from a real run by `e2e.rs` (writing to
//! `PG_RECORD_DIR`), with host identity scrubbed. Replay only re-executes the
//! workflow code against the recorded event stream, so it catches any
//! non-deterministic edit before it reaches a live worker.

use pg_worker::activities::{CardActivities, CardInput, CardOutput, Tick};
use pg_worker::workflow::Card;
use std::time::Duration;
use temporalio_client::WorkflowHistory;
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityOptions, WorkflowContext, WorkflowResult,
    workflow_replayer::{WorkflowReplayer, WorkflowReplayerOptions},
};

/// Load one recorded history from `tests/fixtures/<name>.json`.
///
/// A missing fixture is a hard failure, never a silent pass: a replay test with
/// no evidence to replay would hide a broken recording step.
fn history(name: &str) -> WorkflowHistory {
    let path = format!("{}/tests/fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    WorkflowHistory::from_json(&bytes).expect("parse history json")
}

/// A replayer that knows the production `Card` workflow.
fn replayer() -> WorkflowReplayer {
    WorkflowReplayer::new(
        WorkflowReplayerOptions::new()
            .register_workflow::<Card>()
            .expect("register Card")
            .build(),
    )
    .expect("replayer")
}

#[tokio::test]
async fn replays_happy() {
    replayer()
        .replay_workflow(history("happy"))
        .await
        .expect("happy history replays");
}

#[tokio::test]
async fn replays_contract_defect() {
    replayer()
        .replay_workflow(history("contract_defect"))
        .await
        .expect("contract_defect history replays");
}

#[tokio::test]
async fn replays_check_fails() {
    replayer()
        .replay_workflow(history("check_fails"))
        .await
        .expect("check_fails history replays");
}

/// The gate-block run (a first attempt blocked on the card's check, main left
/// untouched, then a retry that passes and merges) must replay
/// deterministically like any other recorded run.
#[tokio::test]
async fn replays_gate_blocks() {
    replayer()
        .replay_workflow(history("gate_blocks"))
        .await
        .expect("gate_blocks history replays");
}

#[tokio::test]
async fn replays_usage_limit_reroute() {
    replayer()
        .replay_workflow(history("usage_limit_reroute"))
        .await
        .expect("usage_limit_reroute history replays");
}

#[tokio::test]
async fn replays_needs_input_answered() {
    replayer()
        .replay_workflow(history("needs_input_answered"))
        .await
        .expect("needs_input_answered history replays");
}

#[tokio::test]
async fn replays_needs_input_timeout() {
    replayer()
        .replay_workflow(history("needs_input_timeout"))
        .await
        .expect("needs_input_timeout history replays");
}

#[tokio::test]
async fn replays_needs_input_timeout_midwait() {
    replayer()
        .replay_workflow(history("needs_input_timeout_midwait"))
        .await
        .expect("needs_input_timeout_midwait history replays");
}

/// The mid-wait fixture must stop on the durable TimerStarted the restarted
/// worker resumes, with the question deadline as its fire timeout.
#[test]
fn midwait_timer_matches_question_deadline() {
    let path = format!(
        "{}/tests/fixtures/needs_input_timeout_midwait.json",
        env!("CARGO_MANIFEST_DIR")
    );
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    let doc: serde_json::Value = serde_json::from_slice(&bytes).expect("parse midwait json");
    let timer = doc["events"]
        .as_array()
        .expect("events array")
        .iter()
        .find(|e| e["event_type"].as_i64() == Some(17))
        .expect("a TimerStarted event");
    assert_eq!(
        timer["attributes"]["TimerStartedEventAttributes"]["start_to_fire_timeout"].as_str(),
        Some("2.000000000s"),
        "the resumed timer fires at the 2s question deadline"
    );
}

#[tokio::test]
async fn replays_cancel() {
    replayer()
        .replay_workflow(history("cancel"))
        .await
        .expect("cancel history replays");
}

/// Same workflow type name as `Card`, but the run now sleeps before its first
/// activity. Replay must flag this as a non-deterministic change.
#[workflow]
#[derive(Default)]
pub struct CardDrifted;

#[workflow_methods]
impl CardDrifted {
    #[run(name = "Card")]
    pub async fn run(
        ctx: &mut WorkflowContext<Self>,
        _input: CardInput,
    ) -> WorkflowResult<CardOutput> {
        ctx.timer(Duration::from_secs(1)).await; // <- the incompatible change
        let _tick: Tick = ctx
            .execute_activity(
                CardActivities::tick,
                (),
                ActivityOptions::start_to_close_timeout(Duration::from_secs(60)),
            )
            .await?;
        Ok(CardOutput {
            outcome: "done".to_string(),
            detail: "merged".to_string(),
            merged_sha: None,
            attempts: 0,
        })
    }
}

#[tokio::test]
async fn drifted_code_fails_replay() {
    let r = WorkflowReplayer::new(
        WorkflowReplayerOptions::new()
            .register_workflow::<CardDrifted>()
            .expect("register drifted")
            .build(),
    )
    .expect("replayer");
    let res = r.replay_workflow(history("happy")).await;
    assert!(res.is_err(), "non-deterministic change must fail replay");
}
