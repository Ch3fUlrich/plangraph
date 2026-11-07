//! Shared absolute deadline conversion.
pub(crate) fn deadline(started_ms: u64, max_s: Option<u64>) -> Option<u64> {
    let deadline = max_s?
        .checked_mul(1000)
        .and_then(|ms| started_ms.checked_add(ms));
    if deadline.is_none() {
        eprintln!("reconciliation deadline overflow; max-time diagnostic disabled");
    }
    deadline
}
#[cfg(test)]
#[test]
fn reconcile_deadline_units_and_overflow() {
    assert_eq!(deadline(500, Some(2)), Some(2500));
    assert_eq!(deadline(0, None), None);
    assert_eq!(deadline(0, Some(u64::MAX)), None);
    assert_eq!(deadline(u64::MAX, Some(1)), None);
}
