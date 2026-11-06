use super::*;

/// Native parent cancellation must reach an already running Card child.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn phase_cancel_stops_running_child() {
    in_local(async {
        let marker = started_marker();
        let Some(env) = env_or_skip("phase-cancel", &started_script(&marker)).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let key = format!("phase-{}", unique());
        let card = format!("cancel-card-{}", unique());
        env.keeper.push_plan(plan_json(&key, &[(&card, false)]));
        env.keeper
            .push_card_claim(env.claim(&card, &["out.txt"], "test -f out.txt"));
        let handle = env
            .start_phase(&format!("cancel-{}", unique()), env.phase_input(&key, 1))
            .await;
        wait_for_marker(&marker).await;
        handle
            .cancel(Default::default())
            .await
            .expect("cancel Phase");
        let result = tokio::time::timeout(
            CANCEL_BUDGET,
            handle.get_result(WorkflowGetResultOptions::default()),
        )
        .await
        .expect("Phase cancellation finishes within budget");
        assert!(
            result.is_err(),
            "cancelled Phase must not complete successfully"
        );
        assert_child_gone(&marker).await;
        assert!(env.keeper.completed().is_empty());
        assert_eq!(git(&env.repo, &["rev-parse", "main"]), env.base_sha);
        std::fs::remove_file(marker).expect("remove marker");
    })
    .await;
}
