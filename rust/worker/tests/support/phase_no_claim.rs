#[tokio::test]
async fn phase_card_without_claim_is_not_merged() {
    in_local(async {
        let Some(env) = env_or_skip("phase-no-claim", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-no-claim-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper.push_plan(plan_json(&phase_key, &[(card.as_str(), false)]));
        let id = format!("e2e-phase-no-claim-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 2)).await;
        let out = finish_phase("phase_no_claim", &handle).await;
        assert!(pg_worker_core::git::git(&env.repo, &[
            "rev-parse", "--verify", &format!("refs/heads/phase/{phase_key}")
        ]).is_err(), "no claim must create no branch");
        assert_eq!(out.outcome, "partial", "{out:?}");
        assert_eq!(out.not_merged, vec![card.clone()], "{out:?}");
        assert!(out.merged.is_empty(), "{out:?}");
    }).await;
}
