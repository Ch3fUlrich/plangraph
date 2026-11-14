use pg_reap::retention::{decide, Action, Outcome};
#[test]
fn outcomes_and_protection_boundaries() {
    assert_eq!(
        decide(Outcome::Merged, 0, false, false, false),
        Action::RemoveAndDeleteBranch
    );
    assert_eq!(
        decide(Outcome::Succeeded, 100, false, false, false),
        Action::PushAndRetain
    );
    for outcome in [Outcome::Failed, Outcome::Cancelled] {
        assert_eq!(
            decide(outcome, 14 * 86400 - 1, false, false, false),
            Action::RemoveAndKeepBranch
        );
        assert_eq!(
            decide(outcome, 14 * 86400, false, false, false),
            Action::RemoveAndDeleteBranch
        );
        for (pr, claim, dirty) in [(true, false, false), (false, true, false)] {
            assert_eq!(
                decide(outcome, u64::MAX, pr, claim, dirty),
                Action::RemoveAndKeepBranch
            );
        }
    }
    assert_eq!(
        decide(Outcome::Merged, 0, false, false, true),
        Action::BackupRequired
    );
    assert_eq!(
        decide(Outcome::Running, u64::MAX, false, false, false),
        Action::Retain
    );
}
