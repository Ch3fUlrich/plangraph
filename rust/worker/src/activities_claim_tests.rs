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
    crate::card_admit::tests::assert_failure(error, Some("card_not_allowlisted"), true);
    assert!(keeper.claim_card_calls().is_empty());
}

#[test]
fn empty_claim_never_starts_attempt() {
    struct Never(std::sync::atomic::AtomicUsize);
    impl Harness for Never {
        fn name(&self) -> &str { "never" }
        fn command(&self, _: &HarnessRun) -> Command {
            self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            panic!("empty claim invoked harness")
        }
    }
    let keeper = StubKeeper::default();
    let cfg = Config::default();
    let root = tempfile::tempdir().unwrap();
    let harness = Never(std::sync::atomic::AtomicUsize::new(0));
    let result = admitted_claim(&keeper, &cfg, "empty-claim-entry-s2").map(|claim| {
        let input = AttemptInput {
            claim: claim.expect("admitted claim"), n: 1,
            leg: LegSpec { id: "test".into(), tier: pg_worker_core::config::Tier::Trial,
                model: "test".into(), harness: "never".into() },
            workroot: root.path().display().to_string(), class: String::new(), answers: vec![],
        };
        crate::attempt::run_attempt(&harness, &cfg, &Patterns::default(), &input, &|| false)
    });
    crate::card_admit::tests::assert_failure(result.unwrap_err(), Some("card_not_claimable"), true);
    assert_eq!(harness.0.load(std::sync::atomic::Ordering::SeqCst), 0);
    assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
    assert_eq!(keeper.claim_card_calls().len(), 1);
}
