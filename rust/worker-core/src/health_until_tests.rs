use super::*;
use crate::{
    health::{State, UntilSrc},
    outcome::Class,
};
fn row(class: Class, ts: i64) -> Row {
    Row {
        leg_key: "leg".into(),
        route_key: "route".into(),
        role: Role::Review,
        ts,
        outcome: class,
        run_id: ts.to_string(),
        attempt: 1,
        duration_s: None,
        reset_at: None,
        reset_assumed: false,
        reset_clamped: true,
        kind: None,
    }
}
fn fold(rows: &[Row], now: i64, p: Option<&ProviderState>) -> Health {
    fold_with_provider("leg", "route", Role::Review, rows, now, p)
}
fn provider(until: Option<i64>, observed_s: i64) -> ProviderState {
    ProviderState { until, observed_s }
}
#[test]
fn raw_response_clamps_from_row_not_now_all_hard_classes() {
    for class in [
        Class::Quota,
        Class::RateLimit,
        Class::Auth,
        Class::ModelUnavailable,
        Class::ProviderUnavailable,
    ] {
        let mut r = row(class, 10);
        r.reset_at = Some(300000);
        for now in [10, 70] {
            let h = fold(&[r.clone()], now, None);
            assert_eq!(h.until_s, Some(86410));
            assert_eq!(h.until_src, Some(UntilSrc::Row));
            assert!(h.reset_clamped);
            assert!(!h.reset_assumed);
        }
        assert_eq!(r.reset_at, Some(300000));
    }
}
#[test]
fn exactly_24h_not_clamped() {
    let mut r = row(Class::Quota, 10);
    r.reset_at = Some(86410);
    assert!(!fold(&[r], 10, None).reset_clamped);
}
#[test]
fn provider_three_days_unchanged_at_now_plus_60() {
    let p = provider(Some(259200), 0);
    for now in [0, 60] {
        let h = fold(&[], now, Some(&p));
        assert_eq!(h.until_s, Some(259200));
        assert!(!h.reset_clamped);
        assert_eq!(h.until_src, Some(UntilSrc::OmniRoute));
    }
}
#[test]
fn fresher_either_direction_tie_response() {
    let mut r = row(Class::RateLimit, 10);
    r.reset_at = Some(500);
    for until in [100, 1000] {
        assert_eq!(
            fold(&[r.clone()], 20, Some(&provider(Some(until), 11))).until_s,
            Some(until)
        );
        for observed in [9, 10] {
            assert_eq!(
                fold(&[r.clone()], 20, Some(&provider(Some(until), observed))).until_s,
                Some(500)
            );
        }
    }
}
#[test]
fn unchanged_reread_not_fresher() {
    let mut r = row(Class::Auth, 10);
    r.reset_at = Some(500);
    let p = provider(Some(1000), 9);
    assert_eq!(fold(&[r.clone()], 20, Some(&p)), fold(&[r], 80, Some(&p)));
}
#[test]
fn provider_expired_first_read_half_open() {
    assert_eq!(
        fold(&[], 100, Some(&provider(Some(50), 0))).state,
        State::HalfOpen
    );
}
#[test]
fn no_time_fixed_anchor_and_settlement() {
    let p = provider(None, 10);
    for now in [10, 70] {
        assert_eq!(fold(&[], now, Some(&p)).until_s, Some(86410));
    }
    assert_eq!(
        fold(&[row(Class::Ok, 86410)], 90000, Some(&p)),
        Health::default()
    );
}
#[test]
fn success_failure_neutral_settle_value() {
    let p = provider(Some(100), 0);
    assert_eq!(
        fold(&[row(Class::Ok, 100)], 200, Some(&p)),
        Health::default()
    );
    let h = fold(&[row(Class::HangSilent, 100)], 101, Some(&p));
    assert_eq!(h.until_s, Some(400));
    assert_eq!(h.until_src, Some(UntilSrc::Default));
    let rows = [
        row(Class::MaxTime, 100),
        row(Class::MaxTime, 160),
        row(Class::MaxTime, 220),
    ];
    assert_eq!(fold(&rows[..1], 100, Some(&p)).until_s, Some(160));
    assert_eq!(fold(&rows, 220, Some(&p)).state, State::Open);
    assert_eq!(
        fold(&[row(Class::Ok, 100)], 201, Some(&provider(Some(300), 101))).state,
        State::Open
    );
}
#[test]
fn cleared_provider_only_closed_own_rows_remain() {
    assert_eq!(fold(&[], 20, None), Health::default());
    let rows = [row(Class::HangSilent, 1)];
    assert_eq!(fold(&rows, 20, None).until_s, Some(301));
    assert_eq!(
        fold(&rows, 20, Some(&provider(Some(900), 2))).until_s,
        Some(900)
    );
}
#[test]
fn kind_rows_skipped() {
    for kind in ["canary", "omniroute_until", "omniroute_status"] {
        let mut r = row(Class::Ok, 300);
        r.kind = Some(kind.into());
        let rows = [row(Class::HangSilent, 0), r];
        let h = fold(&rows, 300, None);
        assert_eq!((h.state, h.non_verdict_count), (State::HalfOpen, 0));
    }
}
#[test]
fn named_observations_only_preserve_neutral_outcomes() {
    let p = provider(Some(100), 0);
    let mut neutral = row(Class::MaxTime, 100);
    neutral.kind = Some("observation".into());
    let h = fold(&[neutral], 100, Some(&p));
    assert_eq!(
        (h.state, h.non_verdict_count, h.until_s),
        (State::HalfOpen, 1, Some(160))
    );
    let mut success = row(Class::Ok, 100);
    success.kind = Some("arbitrary".into());
    assert_eq!(fold(&[success], 100, Some(&p)).non_verdict_count, 1);
}
#[test]
fn far_reset_probe_24_48_72() {
    let mut r = row(Class::RateLimit, 0);
    r.reset_at = Some(259200);
    let mut rows = vec![r];
    for day in 1..=3 {
        assert_eq!(fold(&rows, day * 86400, None).state, State::HalfOpen);
        if day < 3 {
            rows.push(row(Class::Empty, day * 86400));
            assert_eq!(
                fold(&rows, day * 86400, None).until_s,
                Some((day + 1) * 86400)
            );
        }
    }
}
#[test]
fn shuffled_incident_reset_metadata() {
    let mut a = row(Class::Auth, 10);
    a.reset_at = Some(100);
    let mut b = row(Class::Auth, 20);
    b.duration_s = Some(20);
    b.reset_at = Some(200);
    let mut c = row(Class::Auth, 30);
    c.duration_s = Some(15);
    let expected = fold(&[a.clone(), b.clone(), c.clone()], 30, None);
    assert_eq!(expected.until_s, Some(200));
    assert_eq!(expected.strikes, 1);
    for rows in [[c.clone(), a.clone(), b.clone()], [b, c, a]] {
        assert_eq!(fold(&rows, 30, None), expected);
    }
}
#[test]
fn saturation_invalid_time_no_panic() {
    for ts in [i64::MIN, i64::MAX] {
        let mut r = row(Class::Quota, ts);
        r.reset_at = Some(i64::MAX);
        assert_eq!(fold(&[r], ts, None).until_src, Some(UntilSrc::Default));
        assert!(fold(&[], ts, Some(&provider(None, ts))).until_s.is_some());
    }
}
#[test]
fn route_role_filter_and_future_rows() {
    let mut r = row(Class::Auth, 10);
    assert_eq!(fold(&[r.clone()], 9, None), Health::default());
    r.route_key = "other".into();
    assert_eq!(fold(&[r.clone()], 20, None), Health::default());
    r.route_key = "route".into();
    r.role = Role::Implement;
    assert_eq!(fold(&[r], 20, None), Health::default());
}
