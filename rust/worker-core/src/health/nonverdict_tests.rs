use super::{
    tests::{health, row},
    *,
};
use crate::outcome::Terminal;

#[test]
fn neutral_probe_loop_and_cap() {
    for class in [
        Class::MaxTime,
        Class::Neutral(Terminal::Cancelled),
        Class::Neutral(Terminal::CallerFault),
        Class::Neutral(Terminal::Orphaned),
        Class::Neutral(Terminal::KillIncomplete),
        Class::Neutral(Terminal::ProofFailure),
        Class::Neutral(Terminal::GitConfigMutated),
        Class::Neutral(Terminal::WriterNoChanges),
    ] {
        let mut rows = vec![row(Class::RateLimit, 0)];
        let mut ts = 300;
        for n in 1..=9 {
            for count in 1..=3 {
                rows.push(row(class, ts));
                let h = health(&rows, ts);
                assert_eq!(h.strikes, 1);
                if count < 3 {
                    assert_eq!(
                        (h.state, h.n, h.non_verdict_count),
                        (State::HalfOpen, n, count)
                    );
                    assert_eq!(h.until_s, Some(ts + 60));
                    assert!(!h.available(ts));
                    ts += 60;
                } else {
                    assert_eq!((h.state, h.n, h.non_verdict_count), (State::Open, n + 1, 0));
                    assert_eq!(h.until_s, Some(ts + backoff_s(n + 1)));
                    ts += backoff_s(n + 1);
                }
            }
        }
    }
}

#[test]
fn verdict_resets_nonverdicts() {
    let rows = [
        row(Class::RateLimit, 0),
        row(Class::MaxTime, 300),
        row(Class::Empty, 360),
        row(Class::MaxTime, 960),
    ];
    let h = health(&rows, 960);
    assert_eq!((h.state, h.n, h.non_verdict_count), (State::HalfOpen, 2, 1));
    let mut rows = rows.to_vec();
    rows.push(row(Class::Ok, 1020));
    assert_eq!(health(&rows, 1020), Health::default());
}

#[test]
fn neutral_never_strikes_or_connects_incidents() {
    let mut bridge = row(Class::MaxTime, 20);
    bridge.duration_s = Some(20);
    let rows = [row(Class::Empty, 1), bridge, row(Class::Empty, 21)];
    assert_eq!(health(&rows, 21).strikes, 2);
    assert_eq!(health(&[row(Class::MaxTime, 1)], 1), Health::default());
}

#[test]
fn scope_and_observations() {
    for class in [
        Class::Auth,
        Class::ProviderUnavailable,
        Class::Quota,
        Class::RateLimit,
    ] {
        let mut r = row(class, 1);
        r.leg_key = "sibling".into();
        assert_eq!(
            health(&[r.clone()], 1).state == State::Open,
            class.scope() == Scope::Route
        );
        r.route_key = "elsewhere".into();
        assert_eq!(health(&[r], 1), Health::default());
    }
    for class in [Class::ToolCallOnly, Class::NoVerdict] {
        let rows: Vec<_> = (1..=3).map(|ts| row(class, ts)).collect();
        assert_eq!(health(&rows, 3).state, State::Open);
        assert_eq!(
            fold("leg", "route", Role::Implement, &rows, 3),
            Health::default()
        );
    }
    let mut r = row(Class::RateLimit, 1);
    r.kind = Some("canary".into());
    assert_eq!(health(&[r], 1), Health::default());
}

#[test]
fn killed_hour_counts_incidents() {
    let mut a = row(Class::KilledExternal, 10);
    a.duration_s = Some(10);
    let mut b = row(Class::KilledExternal, 20);
    b.duration_s = Some(10);
    assert_eq!(health(&[a.clone(), b.clone()], 20).state, State::Closed);
    assert_eq!(
        health(
            &[a.clone(), b.clone(), row(Class::KilledExternal, 3621)],
            3621
        )
        .state,
        State::Closed
    );
    assert_eq!(
        health(&[a, b, row(Class::KilledExternal, 3620)], 3620).state,
        State::Open
    );
}

#[test]
fn stability_shuffle_and_keeper_1103() {
    let rows = vec![
        row(Class::RateLimit, 10),
        row(Class::MaxTime, 310),
        row(Class::Empty, 370),
        row(Class::MaxTime, 970),
    ];
    let expected = health(&rows, 970);
    for shift in 0..rows.len() {
        let mut shuffled = rows.clone();
        shuffled.rotate_left(shift);
        shuffled.reverse();
        assert_eq!(health(&shuffled, 970), expected);
    }
    assert_eq!(health(&rows, 970 + 3 * 86400).until_s, expected.until_s);
    let r = [row(Class::RateLimit, 100)];
    assert!(!health(&r, 399).available(399));
    assert_eq!(health(&r, 400).state, State::HalfOpen);
    assert!(health(&r, 400).available(400));
}

#[test]
fn duplicates_last_row_wins() {
    let a = row(Class::RateLimit, 1);
    let mut b = row(Class::Ok, 2);
    b.run_id = a.run_id.clone();
    assert_eq!(health(&[a, b], 2), Health::default());
}

#[test]
fn interval_start_sort_and_latest_anchor() {
    let mut long = row(Class::Empty, 100);
    long.duration_s = Some(100);
    let point = row(Class::Empty, 30);
    let mut tail = row(Class::Empty, 120);
    tail.duration_s = Some(30);
    let rows = [tail, point, long, row(Class::Empty, 121)];
    let h = health(&rows, 121);
    assert_eq!((h.strikes, h.until_s), (2, Some(421)));
    let mut reversed = rows.to_vec();
    reversed.reverse();
    assert_eq!(health(&reversed, 121), h);
}

#[test]
fn success_closes_and_fresh_success_protects_probe() {
    let rows = [
        row(Class::Ok, 0),
        row(Class::RateLimit, 1),
        row(Class::Empty, 301),
    ];
    let h = health(&rows, 301);
    assert_eq!((h.state, h.n), (State::HalfOpen, 1));
    let rows = [row(Class::ToolCallOnly, 1), row(Class::Ok, 2)];
    assert_eq!(health(&rows, 2), Health::default());
}
