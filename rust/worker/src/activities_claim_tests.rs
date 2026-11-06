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
        ..Config::allow_all_for_tests()
    };
    let error = admitted_claim(&keeper, &cfg, "unlisted").unwrap_err();
    crate::card_admit::tests::assert_failure(error, Some("card_not_allowlisted"), true);
    assert!(keeper.claim_card_calls().is_empty());
}

#[test]
fn empty_claim_is_terminal_refusal() {
    let keeper = StubKeeper::default();
    let cfg = Config::allow_all_for_tests();
    let result = admitted_claim(&keeper, &cfg, "empty-claim-entry-s2");
    crate::card_admit::tests::assert_failure(result.unwrap_err(), Some("card_not_claimable"), true);
    assert_eq!(keeper.claim_card_calls().len(), 1);
}
