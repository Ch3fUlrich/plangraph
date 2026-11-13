use super::*;
use std::cell::Cell;

#[test]
fn completed_collection_is_not_retimed_at_the_deadline_boundary() {
    let deadline = Instant::now();
    let samples = Cell::new(0);
    let ready = Cell::new(false);
    let now = || {
        samples.set(samples.get() + 1);
        if samples.get() > 2 {
            deadline
        } else {
            deadline - Duration::from_nanos(1)
        }
    };
    let completed =
        wait_before_deadline(deadline, now, |_| ready.set(true), || Ok(ready.get())).unwrap();
    assert!(completed);
    assert!(ready.get(), "must have waited for completion");
    assert_eq!(samples.get(), 2, "do not resample after observing success");
}

#[test]
fn completion_at_or_after_the_deadline_gets_no_grace() {
    let deadline = Instant::now();
    for delay in [Duration::ZERO, Duration::from_nanos(1)] {
        let completed = wait_before_deadline(
            deadline,
            || deadline + delay,
            |_| panic!("deadline cannot add a sleep"),
            || Ok(true),
        )
        .unwrap();
        assert!(!completed);
    }
}
