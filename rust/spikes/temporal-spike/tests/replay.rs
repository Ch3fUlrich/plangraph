//! Offline replay tests: no Temporal server, no network. The histories in
//! `tests/fixtures` were recorded from a real dev-server run by
//! `driver all tests/fixtures` (host identity scrubbed).

use std::time::Duration;
use temporal_spike::{AttemptInput, CardInput, CardOutput, activities::CardActivities, card::Card};
use temporalio_client::WorkflowHistory;
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityOptions, WorkflowContext, WorkflowResult,
    workflow_replayer::{WorkflowReplayer, WorkflowReplayerOptions},
};

fn history(name: &str) -> WorkflowHistory {
    let path = format!("{}/tests/fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    WorkflowHistory::from_json(&bytes).expect("parse history json")
}

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
async fn replays_answer_run() {
    replayer()
        .replay_workflow(history("answer"))
        .await
        .expect("answer history replays");
}

#[tokio::test]
async fn replays_deadline_default_run() {
    replayer()
        .replay_workflow(history("default"))
        .await
        .expect("default history replays");
}

/// Same workflow type name, but the code now sleeps before the activity.
/// Replay must flag this as a non-deterministic change.
#[workflow]
#[derive(Default)]
pub struct CardDrifted;

#[workflow_methods]
impl CardDrifted {
    #[run(name = "Card")]
    pub async fn run(
        ctx: &mut WorkflowContext<Self>,
        input: CardInput,
    ) -> WorkflowResult<CardOutput> {
        ctx.timer(Duration::from_secs(1)).await; // <- the incompatible change
        let attempt = ctx
            .execute_activity(
                CardActivities::attempt,
                AttemptInput {
                    mode: input.mode,
                    steps: 4,
                },
                ActivityOptions::start_to_close_timeout(Duration::from_secs(60)),
            )
            .await?;
        Ok(CardOutput {
            attempt,
            resolution: "default".into(),
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
    let res = r.replay_workflow(history("default")).await;
    assert!(res.is_err(), "non-deterministic change must fail replay");
}
