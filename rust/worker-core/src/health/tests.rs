use super::*;

#[test]
fn non_verdict_probe_loop() {
    let mut rows = vec![row(Class::HangSilent, 0)];
    for cycle in 1..=7 {
        let due = health(&rows, rows.last().unwrap().ts).until_s.unwrap();
        for count in 1..=3 {
            let ts = due + (count - 1) * 60;
            let mut observation = row(Class::HangSilent, ts);
            observation.kind = Some("observation".into());
            observation.run_id = "probe".into();
            observation.duration_s = Some(7200);
            rows.push(observation);
            let h = health(&rows, ts);
            let (state, n, pending, gap) = if count < 3 {
                (State::HalfOpen, cycle, count as u32, 60)
            } else {
                (State::Open, cycle + 1, 0, backoff_s(cycle + 1))
            };
            assert_eq!((h.state, h.n, h.non_verdict_count), (state, n, pending));
            assert_eq!(h.until_s, Some(ts + gap));
            let mut reversed = rows.clone();
            reversed.reverse();
            assert_eq!(health(&reversed, ts), h);
            assert_eq!(health(&rows, ts), h);
        }
    }
}

#[test]
fn non_verdict_verdict_restarts_count() {
    let mut rows = vec![row(Class::HangSilent, 0)];
    for (ts, kind, class, expected) in [
        (300, true, Class::Ok, 1),
        (360, false, Class::Empty, 0),
        (960, true, Class::Empty, 1),
        (1020, false, Class::Ok, 0),
        (1080, true, Class::HangSilent, 0),
    ] {
        let mut r = row(class, ts);
        r.kind = kind.then(|| "observation".into());
        rows.push(r);
        assert_eq!(health(&rows, ts).non_verdict_count, expected);
    }
    assert_eq!(health(&rows, 1080), Health::default());
}

#[test]
fn dedupe_preserves_other_scopes() {
    let a = row(Class::Empty, 10);
    for scope in ["leg", "route"] {
        let mut b = a.clone();
        b.ts = 11;
        if scope == "leg" {
            b.leg_key = "other".into();
        } else {
            b.route_key = "other".into();
        }
        assert_eq!(health(&[a.clone(), b], 10).strikes, 1);
    }
}

#[test]
fn duplicate_order_is_stable() {
    let a = row(Class::Empty, 10);
    for ts in [10, 20] {
        let mut b = a.clone();
        b.ts = ts;
        b.outcome = Class::Ok;
        let rows = [a.clone(), b.clone()];
        let expected = Health::default();
        assert_eq!(health(&rows, 20), expected);
        assert_eq!(health(&[b, a.clone()], 20), expected);
        assert_eq!(health(&rows, 20), health(&rows, 20));
    }
}

#[test]
fn role_scope_applies_to_every_class() {
    let mut rows = [row(Class::Empty, 1), row(Class::Empty, 2)];
    for r in &mut rows {
        r.role = Role::Implement;
    }
    assert_eq!(health(&rows, 2), Health::default());
    rows[0].role = Role::Review;
    rows[1].outcome = Class::Ok;
    assert_eq!(health(&rows, 2).strikes, 1);
}

#[test]
fn recent_success_probe_exception_and_open_escalation() {
    for (ts, state, n, until) in [
        (301, State::HalfOpen, 1, 301),
        (86400, State::Open, 2, 87000),
    ] {
        let rows = [
            row(Class::Ok, 0),
            row(Class::HangSilent, 1),
            row(Class::Empty, ts),
        ];
        let h = health(&rows, ts);
        assert_eq!((h.state, h.n, h.until_s), (state, n, Some(until)));
    }
    let h = health(&[row(Class::HangSilent, 1), row(Class::Empty, 2)], 2);
    assert_eq!((h.state, h.n, h.until_s), (State::Open, 2, Some(602)));
}

pub(super) fn row(class: Class, ts: i64) -> Row {
    Row {
        leg_key: if class.scope() == Scope::LegReview {
            "leg#review"
        } else {
            "leg"
        }
        .into(),
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
fn until_source_is_always_class_default() {
    for class in [Class::RateLimit, Class::HangSilent] {
        assert_eq!(
            health(&[row(class, 1)], 1).until_src,
            Some(UntilSrc::Default)
        );
    }
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
