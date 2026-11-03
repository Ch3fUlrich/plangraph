use super::*;

pub(super) fn row(class: Class, ts: i64) -> Row {
    Row {
        leg_key: "leg".into(),
        route_key: "route".into(),
        ts,
        role: Role::Review,
        outcome: class,
        run_id: ts.to_string(),
        attempt: 1,
        duration_s: None,
        reset_at: Some(999999),
        reset_assumed: false,
        reset_clamped: true,
        kind: None,
    }
}
pub(super) fn health(rows: &[Row], now: i64) -> Health {
    fold("leg", "route", Role::Review, rows, now)
}

#[test]
fn table_defaults_and_clock() {
    for (class, count) in [
        (Class::HangNoActivity, 1),
        (Class::HangSilent, 1),
        (Class::NoProgress, 3),
        (Class::KilledExternal, 2),
        (Class::Empty, 2),
        (Class::Crash, 2),
        (Class::ToolCallOnly, 1),
        (Class::NoVerdict, 3),
        (Class::ModelUnavailable, 1),
        (Class::ProviderUnavailable, 1),
        (Class::Quota, 1),
        (Class::RateLimit, 1),
        (Class::Auth, 1),
    ] {
        let rows: Vec<_> = (1..=count).map(|n| row(class, n)).collect();
        if count > 1 {
            assert_eq!(
                health(&rows[..count as usize - 1], count).state,
                State::Closed
            );
        }
        let h = health(&rows, count);
        assert_eq!(h.state, State::Open, "{class:?}");
        assert_eq!(
            h.until_s,
            Some(if class == Class::Quota {
                86400
            } else {
                count + 300
            })
        );
        assert_eq!(h.reset_assumed, class == Class::Quota);
        assert!(!h.reset_clamped);
        assert_eq!(h.heuristic, !class.carries_reset());
        assert_eq!(health(&rows, h.until_s.unwrap()).state, State::HalfOpen);
    }
}
#[test]
fn day_and_backoff_bounds() {
    for (ts, expected) in [(-1, 0), (0, 86400), (86399, 86400), (86400, 172800)] {
        assert_eq!(next_utc_midnight(ts), expected);
        assert_eq!(health(&[row(Class::Quota, ts)], ts).until_s, Some(expected));
    }
    assert_eq!(
        (1..=7).map(backoff_s).collect::<Vec<_>>(),
        vec![300, 600, 1200, 2400, 4800, 7200, 7200]
    );
    assert_eq!(backoff_s(u32::MAX), 7200);
}
#[test]
fn success_and_failed_probe() {
    let mut rows = vec![row(Class::HangSilent, 10), row(Class::Empty, 310)];
    let h = health(&rows, 310);
    assert_eq!((h.state, h.n, h.until_s), (State::Open, 2, Some(910)));
    rows.push(row(Class::Ok, 311));
    assert_eq!(health(&rows, 312), Health::default());
    assert_eq!(
        health(&[row(Class::HangSilent, 10), row(Class::Ok, 11)], 12),
        Health::default()
    );
}
#[test]
fn incident_cases() {
    let mut a = row(Class::Empty, 10);
    a.duration_s = Some(10);
    let mut b = row(Class::Empty, 20);
    b.duration_s = Some(10);
    let mut c = row(Class::Empty, 30);
    c.duration_s = Some(10);
    let h = health(&[c.clone(), a.clone(), b.clone()], 30);
    assert_eq!((h.strikes, h.state), (1, State::Closed));
    c.ts = 31;
    c.duration_s = None;
    assert_eq!(health(&[a.clone(), b.clone(), c], 31).state, State::Open);
    b.outcome = Class::Crash;
    assert_eq!(health(&[a, b], 20).strikes, 2);
    let rows: Vec<_> = (1..=3)
        .map(|n| {
            let mut r = row(Class::NoVerdict, n);
            r.duration_s = Some(10);
            r
        })
        .collect();
    assert_eq!(health(&rows, 3).strikes, 1);
    let mut rows = rows;
    rows.push(row(Class::NoVerdict, 4));
    rows.push(row(Class::NoVerdict, 5));
    assert_eq!(health(&rows, 5).until_s, Some(305));
}
