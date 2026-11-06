use super::*;

#[test]
fn phase_outcome_done_when_nothing_ran() {
    assert_eq!(phase_outcome(&[], &[], &[]), OUTCOME_DONE);
}

#[test]
fn phase_outcome_partial_when_not_merged() {
    let not_merged = vec!["b".to_string()];
    assert_eq!(phase_outcome(&[], &not_merged, &[]), OUTCOME_PARTIAL);
}

#[test]
fn phase_outcome_partial_when_blocked_later() {
    let merged = vec!["a".to_string()];
    let blocked = vec!["phase not ready".to_string()];
    assert_eq!(phase_outcome(&merged, &[], &blocked), OUTCOME_PARTIAL);
}

#[test]
fn phase_outcome_blocked_when_nothing_ran_and_blocked() {
    let blocked = vec!["phase not ready".to_string()];
    assert_eq!(phase_outcome(&[], &[], &blocked), OUTCOME_BLOCKED);
}
