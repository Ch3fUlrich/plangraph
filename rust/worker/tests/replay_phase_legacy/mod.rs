use super::*;
use temporalio_workflow::workflow_interceptors::WorkflowInterceptorConstructor;

#[tokio::test]
async fn recorded_pre_patch_phase_branches_replay() {
    for name in ["phase_two_cards", "phase_defer"] {
        assert!(!fixture_patch_markers(name).contains("phase-branch-hash-v1"));
        // Core ignores payload differences. The output interceptor below checks
        // the actual workflow result against the recorded legacy branch, making
        // removal of patched selection fail without changing legacy commands.
        let mut doc = fixture_json(name);
        expand_data_json(&mut doc);
        // Require recorded branch commands, not just a blocked/no-op history.
        let mut branches = Vec::new();
        for event in doc["events"].as_array().unwrap() {
            let attrs = &event["attributes"]["ActivityTaskScheduledEventAttributes"];
            let activity = attrs["activity_type"]["name"].as_str().unwrap_or("");
            if activity.ends_with("ensure_phase_branch") || activity.ends_with("phase_finish") {
                let payload = &attrs["input"]["payloads"][0];
                let input: serde_json::Value = serde_json::from_slice(&base64_decode(
                    payload["data"].as_str().expect("branch payload"),
                ))
                .expect("decode branch input");
                let expected = match name {
                    "phase_two_cards" => "phase/phase-33069540670160152892635464651174313987",
                    "phase_defer" => "phase/phase-33069540525861169132674685342045962243",
                    _ => unreachable!(),
                };
                assert_eq!(input["branch"], expected);
                branches.push(activity.to_owned());
            }
        }
        assert!(
            !branches.is_empty(),
            "history must exercise branch commands"
        );
        let output = super::phase_terminal::terminal_output(&doc);
        WorkflowReplayer::new(
            WorkflowReplayerOptions::new()
                .register_workflow::<Card>()
                .unwrap()
                .register_workflow::<Phase>()
                .unwrap()
                .workflow_interceptor(WorkflowInterceptorConstructor::new(move |_| {
                    super::phase_terminal::AssertTerminal(output.clone())
                }))
                .build(),
        )
        .unwrap()
        .replay_workflow(history(name))
        .await
        .expect("pre-patch Phase replays");
    }
}
