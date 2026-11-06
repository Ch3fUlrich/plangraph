use super::*;

#[tokio::test]
async fn recorded_pre_patch_phase_branches_replay() {
    for name in ["phase_two_cards", "phase_defer"] {
        assert!(!fixture_patch_markers(name).contains("phase-branch-hash-v1"));
        // Temporal replay does not compare activity payload bytes. Pin the
        // legacy selection separately in phase::legacy_tests.
        let mut doc = fixture_json(name);
        expand_data_json(&mut doc);
        // Require recorded branch commands, not just a blocked/no-op history.
        let mut branches = Vec::new();
        for event in doc["events"].as_array().unwrap() {
            let attrs = &event["attributes"]["ActivityTaskScheduledEventAttributes"];
            let activity = attrs["activity_type"]["name"].as_str().unwrap_or("");
            if activity.ends_with("ensure_phase_branch") || activity.ends_with("phase_finish") {
                branches.push(activity.to_owned());
            }
        }
        assert!(
            !branches.is_empty(),
            "history must exercise branch commands"
        );
        replayer()
            .replay_workflow(history(name))
            .await
            .expect("pre-patch Phase replays");
    }
}
