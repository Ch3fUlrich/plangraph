use super::*;

fn assert_cancelled<T: std::fmt::Debug>(
    result: Result<T, temporalio_client::errors::WorkflowGetResultError>,
) {
    assert!(
        matches!(
            result,
            Err(temporalio_client::errors::WorkflowGetResultError::Cancelled { .. })
        ),
        "Phase must finish specifically cancelled, got {result:?}"
    );
}

#[test]
fn timeout_is_not_cancellation() {
    assert!(
        std::panic::catch_unwind(|| {
            assert_cancelled::<()>(Err(
                temporalio_client::errors::WorkflowGetResultError::TimedOut,
            ));
        })
        .is_err()
    );
}

/// Native parent cancellation must reach an already running Card child.
/// Coverage of the existing main implementation: Phase::run starts Card with
/// default child options and awaits started.result(); native SDK cancellation
/// stops the parent and its child. No production command change is required.
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
        assert_cancelled(result);
        assert_child_gone(&marker).await;
        assert!(env.keeper.completed().is_empty());
        assert_eq!(git(&env.repo, &["rev-parse", "main"]), env.base_sha);
        std::fs::remove_file(marker).expect("remove marker");
    })
    .await;
}
