use std::{
    thread,
    time::{Duration, Instant},
};

pub(super) fn until_deadline(deadline: Instant, mut predicate: impl FnMut() -> bool) {
    loop {
        if predicate() {
            break;
        }
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn ready_condition_wins_over_elapsed_poll_deadline() {
    // A descheduled observer must still accept an already satisfied condition.
    until_deadline(Instant::now() - Duration::from_secs(1), || true);
}
