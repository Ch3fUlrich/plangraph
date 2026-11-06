#[test]
fn claim_card_pure_body_uses_keeper() {
    let keeper = StubKeeper::default();
    let claim = Claim {
        card: "c1".to_string(),
        repo: "r".to_string(),
        base_sha: "s".to_string(),
        check_cmd: "true".to_string(),
        allow: vec![],
        forbid: vec![],
        prompt: "p".to_string(),
        ..Default::default()
    };
    keeper.push_card_claim(claim.clone());

    assert_eq!(do_claim_card(&keeper, "c1").unwrap(), Some(claim));
    assert_eq!(
        keeper.claim_card_calls(),
        vec![(merge_owner_id().to_string(), "c1".to_string())]
    );

    let empty = StubKeeper::default();
    assert_eq!(do_claim_card(&empty, "missing").unwrap(), None);
}

#[test]
fn unlisted_claim_never_calls_keeper() {
    let keeper = StubKeeper::default();
    let cfg = Config {
        card_allowlist: Some(["listed".to_owned()].into()),
        ..Config::default()
    };
    let error = admitted_claim(&keeper, &cfg, "unlisted").unwrap_err();
    let text = format!("{error:?}");
    assert!(text.contains("card_not_allowlisted"), "{text}");
    assert!(text.contains("unlisted"), "{text}");
    assert!(text.contains("non_retryable: true"), "{text}");
    assert!(keeper.claim_card_calls().is_empty());
}

#[test]
fn empty_claim_never_starts_attempt() {
    let keeper = StubKeeper::default();
    let cfg = Config::default();
    let result = admitted_claim(&keeper, &cfg, "empty-claim-entry-s2");
    let text = format!("{:?}", result.unwrap_err());
    assert!(text.contains("card_not_claimable"), "{text}");
    assert!(text.contains("empty-claim-entry-s2"), "{text}");
    assert!(text.contains("non_retryable: true"), "{text}");
    let harness = crate::card_admit::tests::CountingHarness::default();
    assert!(crate::card_admit::run_attempt(Some(&harness), &cfg, &Patterns::default(), None, &|| false).is_err());
    assert_eq!(harness.0.load(std::sync::atomic::Ordering::SeqCst), 0);
    assert_eq!(keeper.claim_card_calls().len(), 1);
}
