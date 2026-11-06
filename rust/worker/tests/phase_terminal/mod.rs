use super::*;

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
        replayer().replay_workflow(history(name)).await.expect(name);
    }
}
