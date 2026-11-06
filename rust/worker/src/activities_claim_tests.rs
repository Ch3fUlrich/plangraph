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

#[test]
fn empty_claim_attempt_entry_never_starts_or_sets_up() {
    use crate::attempt::{AttemptInput, LegSpec};
    use crate::harness::{Harness, HarnessRun};
    use pg_worker_core::{config::Tier, exit_class::Patterns};
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct Never(AtomicUsize);
    impl Harness for Never {
        fn name(&self) -> &str { "never" }
        fn command(&self, _: &HarnessRun) -> std::process::Command {
            self.0.fetch_add(1, Ordering::SeqCst);
            std::process::Command::new("false")
        }
    }
    let root = tempfile::tempdir().unwrap();
    let harness = Never(AtomicUsize::new(0));
    let input = AttemptInput {
        claim: Claim::default(), n: 1,
        leg: LegSpec { id: "test".into(), tier: Tier::Trial, model: "test".into(), harness: "never".into() },
        workroot: root.path().to_string_lossy().into_owned(), class: String::new(), answers: vec![],
    };
    // Exercise the actual attempt entry, not an unreachable map after claimed().
    let error = crate::card_admit::run_attempt(Some(&harness), &Config::allow_all_for_tests(), &Patterns::default(), &input, &|| false).unwrap_err();
    crate::card_admit::tests::assert_failure(crate::card_admit::attempt_error(error), Some("card_not_claimable"), true);
    assert_eq!(harness.0.load(Ordering::SeqCst), 0);
    assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
}
