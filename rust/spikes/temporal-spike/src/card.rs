//! The `card` workflow.

use crate::activities::CardActivities;
use crate::{AttemptInput, CardInput, CardOutput};
use std::time::Duration;
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityExecutionError, ActivityOptions, SyncWorkflowContext, WorkflowCancellationToken,
    WorkflowContext, WorkflowResult,
};

#[workflow]
#[derive(Default)]
pub struct Card {
    answer: Option<String>,
    cancel_requested: bool,
    /// Token handed to the activity; the `cancel` signal fires it.
    op: Option<WorkflowCancellationToken>,
}

#[workflow_methods]
impl Card {
    #[run]
    pub async fn run(
        ctx: &mut WorkflowContext<Self>,
        input: CardInput,
    ) -> WorkflowResult<CardOutput> {
        // Child of the workflow token: workflow cancel AND the signal both reach the activity.
        let op = ctx.cancellation_token().child_token();
        ctx.state_mut(|s| s.op = Some(op.clone()));

        let retry = temporalio_common::RetryPolicy::builder()
            .initial_interval(Duration::from_millis(200))
            .maximum_attempts(input.max_attempts)
            .build();
        let opts = ActivityOptions::with_start_to_close_timeout(Duration::from_secs(60))
            .heartbeat_timeout(Duration::from_secs(2)) // production plan: 2 min
            .retry_policy(retry)
            .cancellation_token(op)
            .build();
        let attempt = match ctx
            .execute_activity(
                CardActivities::attempt,
                AttemptInput {
                    mode: input.mode.clone(),
                    steps: 4,
                },
                opts,
            )
            .await
        {
            Ok(v) => v,
            Err(ActivityExecutionError::Cancelled(_)) => {
                if ctx.cancellation_token().is_cancelled() {
                    return Err(temporalio_sdk::WorkflowTermination::cancelled());
                }
                return Ok(CardOutput {
                    attempt: "cancelled".into(),
                    resolution: "cancelled".into(),
                });
            }
            Err(e) => {
                return Ok(CardOutput {
                    attempt: format!("failed: {e:?}"),
                    resolution: "skipped".into(),
                });
            }
        };

        // Question phase: operator answer vs. durable deadline vs. cancel signal.
        let resolution = temporalio_sdk::workflows::select! {
            r = ctx.wait_condition(|s: &Self| s.answer.is_some()) => {
                r?;
                format!("answer:{}", ctx.state(|s| s.answer.clone().unwrap_or_default()))
            }
            r = ctx.wait_condition(|s: &Self| s.cancel_requested) => {
                r?;
                "cancelled".to_string()
            }
            _ = ctx.timer(Duration::from_millis(input.deadline_ms)) => "default".to_string(),
        };
        Ok(CardOutput {
            attempt,
            resolution,
        })
    }

    #[signal]
    pub fn answer(&mut self, _ctx: &mut SyncWorkflowContext<Self>, text: String) {
        self.answer = Some(text);
    }

    #[signal]
    pub fn cancel(&mut self, _ctx: &mut SyncWorkflowContext<Self>, reason: String) {
        self.cancel_requested = true;
        if let Some(op) = &self.op {
            op.cancel_with_reason(reason);
        }
    }
}
