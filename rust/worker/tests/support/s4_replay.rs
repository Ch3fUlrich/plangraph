use super::*;

#[tokio::test]
async fn replays_before_phase_claim_before_branch_v1() {
    assert!(!fixture_patch_markers("phase_two_cards").contains("phase-claim-before-branch-v1"));
    let activities = fixture_activities("phase_two_cards");
    let ensure = activities
        .iter()
        .position(|a| a == "CardActivities::ensure_phase_branch")
        .unwrap();
    let claim = activities
        .iter()
        .position(|a| a == "CardActivities::claim_card")
        .unwrap();
    assert!(
        ensure < claim,
        "fixture must exercise the old command order"
    );
    replayer()
        .replay_workflow(history("phase_two_cards"))
        .await
        .unwrap();
}

#[tokio::test]
async fn replays_before_card_plan_key_memo_v1() {
    assert!(!fixture_patch_markers("happy").contains("card-plan-key-memo-v1"));
    assert!(
        !fixture_json("happy")["events"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["attributes"]
                .get("WorkflowPropertiesModifiedEventAttributes")
                .is_some())
    );
    replayer().replay_workflow(history("happy")).await.unwrap();
}
