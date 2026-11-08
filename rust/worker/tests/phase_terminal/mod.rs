use super::*;
use pg_worker::phase::PhaseOutput;
use temporalio_workflow::workflow_interceptors::*;

pub(super) struct AssertTerminal(pub(super) PhaseOutput);

impl WorkflowInterceptor for AssertTerminal {
    fn execute<'a>(
        &'a self,
        _ctx: WorkflowInterceptorContext,
        input: ExecuteWorkflowInput,
        next: WorkflowNext<
            'a,
            ExecuteWorkflowInput,
            WorkflowInterceptorFuture<'a, ExecuteWorkflowResult>,
        >,
    ) -> WorkflowInterceptorFuture<'a, ExecuteWorkflowResult> {
        WorkflowInterceptorFuture::new(async move {
            let output = next.run(input).await?;
            assert_eq!(output.downcast_ref::<PhaseOutput>(), Some(&self.0));
            Ok(output)
        })
    }
}

pub(super) fn terminal_output(doc: &serde_json::Value) -> PhaseOutput {
    let completed = doc["events"]
        .as_array()
        .unwrap()
        .iter()
        .find_map(|e| e["attributes"].get("WorkflowExecutionCompletedEventAttributes"))
        .expect("completed workflow");
    let payload = &completed["result"]["payloads"][0];
    let value = payload.get("data_json").cloned().unwrap_or_else(|| {
        serde_json::from_slice(&base64_decode(payload["data"].as_str().unwrap())).unwrap()
    });
    serde_json::from_value(value).expect("Phase terminal output")
}

/// Terminal paths must retain their command shape, including absence of child
/// starts when planning is blocked or the keeper declines a claim.
#[tokio::test]
async fn replays_phase_terminal_paths() {
    for (name, expected, children) in [
        ("phase_blocked", vec!["phase_plan"], 0),
        (
            "phase_no_claim",
            vec![
                "phase_plan",
                "ensure_phase_branch",
                "claim_card",
                "phase_plan",
            ],
            0,
        ),
        (
            "phase_wave_cap",
            vec![
                "phase_plan",
                "ensure_phase_branch",
                "claim_card",
                "phase_plan",
            ],
            1,
        ),
    ] {
        let expected: Vec<_> = expected
            .into_iter()
            .map(|a| format!("CardActivities::{a}"))
            .collect();
        assert_eq!(fixture_activities(name), expected, "{name}");
        let doc = fixture_json(name);
        assert_eq!(
            doc["events"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|e| {
                    e["attributes"]
                        .get("StartChildWorkflowExecutionInitiatedEventAttributes")
                        .is_some()
                })
                .count(),
            children,
            "{name}"
        );
        let output = terminal_output(&doc);
        let (outcome, blocked, merged, not_merged, waves) = match name {
            "phase_blocked" => ("blocked", vec!["no opencode leg"], 0, 0, 0),
            "phase_no_claim" => ("partial", vec![], 0, 1, 1),
            "phase_wave_cap" => ("partial", vec!["wave cap reached"], 1, 0, 1),
            _ => unreachable!(),
        };
        assert_eq!(output.outcome, outcome, "{name}");
        assert_eq!(output.blocked, blocked, "{name}");
        assert_eq!(output.merged.len(), merged, "{name}");
        assert_eq!(output.not_merged.len(), not_merged, "{name}");
        assert_eq!(output.waves, waves, "{name}");
        assert_eq!(output.finish, None, "{name}");
        WorkflowReplayer::new(
            WorkflowReplayerOptions::new()
                .register_workflow::<Phase>()
                .unwrap()
                .workflow_interceptor(WorkflowInterceptorConstructor::new(move |_| {
                    AssertTerminal(output.clone())
                }))
                .build(),
        )
        .unwrap()
        .replay_workflow(history(name))
        .await
        .expect(name);
    }
}
