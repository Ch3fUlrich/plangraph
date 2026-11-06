use super::*;
use crate::{
    health::{fold, Row},
    outcome::Class,
};

fn leg(id: &str) -> LegInfo {
    LegInfo {
        leg_key: id.into(),
        family: "family".into(),
        tier: 1,
        route_key: "served".into(),
        model: "model".into(),
    }
}
fn struck(class: Class) -> Health {
    Health {
        state: State::Open,
        heuristic: !class.carries_reset(),
        reason: Some(class),
        until_s: Some(999),
        strikes: 3,
        ..Health::default()
    }
}
fn pair(a: Health, b: Health) -> Vec<(String, Health)> {
    vec![("a".into(), a), ("b".into(), b)]
}
pub(super) fn row(class: Class, ts: i64) -> Row {
    Row {
        leg_key: "a".into(),
        route_key: "served".into(),
        ts,
        role: Role::Review,
        outcome: class,
        run_id: format!("run-{ts}"),
        attempt: 1,
        duration_s: None,
        reset_at: None,
        reset_assumed: false,
        reset_clamped: false,
        kind: None,
    }
}

#[test]
fn two_same_family_struck_one_degraded() {
    let hs = pair(struck(Class::HangSilent), struck(Class::HangSilent));
    let out = family_floor(&hs, &[leg("a"), leg("b")], Role::Review);
    let mut expected = hs.clone();
    expected[0].1.state = State::Degraded;
    assert_eq!(out, expected);
    assert!(out[0].1.available(0));
    assert!(!out[1].1.available(0));
}

#[test]
fn hard_facts_and_quota_never_floored() {
    for class in [
        Class::Quota,
        Class::RateLimit,
        Class::Auth,
        Class::ModelUnavailable,
        Class::ProviderUnavailable,
    ] {
        let mut h = struck(class);
        h.heuristic = true; // Hard facts remain protected even with inconsistent heuristic metadata.
        let hs = vec![("a".into(), h)];
        assert_eq!(family_floor(&hs, &[leg("a")], Role::Review), hs);
    }
    let mut h = struck(Class::HangSilent);
    h.until_src = Some(UntilSrc::OmniRoute);
    let hs = vec![("a".into(), h)];
    assert_eq!(family_floor(&hs, &[leg("a")], Role::Review), hs);
}

#[test]
fn last_healthy_peer_preserved() {
    for state in [State::Closed, State::Degraded] {
        let hs = pair(
            struck(Class::HangSilent),
            Health {
                state,
                ..Health::default()
            },
        );
        assert_eq!(family_floor(&hs, &[leg("a"), leg("b")], Role::Review), hs);
    }
}

#[test]
fn review_tool_only_peer_and_last_leg() {
    let h = fold(
        "a",
        "served",
        Role::Review,
        &[row(Class::ToolCallOnly, 1)],
        2,
    );
    assert!(h.review_unfit);
    assert_eq!(h.state, State::Open);
    let hs = pair(h.clone(), Health::default());
    assert_eq!(family_floor(&hs, &[leg("a"), leg("b")], Role::Review), hs);
    let out = family_floor(&[("a".into(), h)], &[leg("a")], Role::Review);
    assert_eq!(out[0].1.state, State::Degraded);
    assert!(out[0].1.review_unfit);
    let mut r = row(Class::ToolCallOnly, 1);
    r.role = Role::Implement;
    let h = fold("a", "served", Role::Implement, &[r], 2);
    assert_eq!(h.strikes, 1);
    assert_eq!(h.reason, Some(Class::ToolCallOnly));
    assert_eq!(h.state, State::Open);
    assert!(!h.review_unfit);
}

#[test]
fn non_review_tool_only_resets_probe_nonverdict_count() {
    let mut rows = vec![
        row(Class::HangSilent, 1),
        row(Class::MaxTime, 302),
        row(Class::ToolCallOnly, 303),
    ];
    for r in &mut rows {
        r.role = Role::Implement;
    }
    let probe = fold("a", "served", Role::Implement, &rows[..2], 302);
    assert_eq!(probe.state, State::HalfOpen);
    assert_eq!(probe.non_verdict_count, 1);
    let h = fold("a", "served", Role::Implement, &rows, 304);
    assert_eq!(h.non_verdict_count, 0);
    assert_eq!(h.strikes, 2);
    assert_eq!(h.reason, Some(Class::ToolCallOnly));
    assert_eq!(h.state, State::Open);
    assert!(!h.review_unfit);
}

#[test]
fn later_review_ok_clears_unfit() {
    let mut rows = vec![row(Class::ToolCallOnly, 1), row(Class::HangSilent, 2)];
    assert!(fold("a", "served", Role::Review, &rows, 3).review_unfit);
    rows.push(row(Class::Ok, 4));
    assert_eq!(
        fold("a", "served", Role::Review, &rows, 5),
        Health::default()
    );
}

#[test]
fn ok_degraded_reprobe_stable_tiers() {
    let mut plan: Vec<_> = ["open", "probe", "degraded", "ok", "tie", "later"]
        .map(leg)
        .into();
    plan[5].tier = 2;
    let hs = vec![
        ("open".into(), struck(Class::Crash)),
        (
            "probe".into(),
            Health {
                state: State::HalfOpen,
                ..Health::default()
            },
        ),
        (
            "degraded".into(),
            Health {
                state: State::Degraded,
                ..Health::default()
            },
        ),
    ];
    let ids: Vec<_> = order_legs(&plan, &hs, Role::Review)
        .into_iter()
        .map(|l| l.leg_key)
        .collect();
    assert_eq!(ids, ["ok", "tie", "degraded", "probe", "open", "later"]);
}

#[test]
fn unfit_after_every_review_peer_in_tier() {
    let mut plan = vec![leg("a"), leg("b"), leg("c")];
    plan[2].tier = 2;
    let hs = pair(
        Health {
            review_unfit: true,
            ..Health::default()
        },
        struck(Class::Crash),
    );
    assert_eq!(
        order_legs(&plan, &hs, Role::Review),
        vec![leg("b"), leg("a"), plan[2].clone()]
    );
    assert_eq!(order_legs(&plan, &hs, Role::Implement), plan);
}

#[test]
fn omniroute_only_cleared_is_ok() {
    let h = crate::health_until::fold_with_provider("a", "served", Role::Review, &[], 10, None);
    assert_eq!(h, Health::default());
    assert_eq!(
        order_legs(
            &[leg("a"), leg("b")],
            &pair(h, struck(Class::Crash)),
            Role::Review
        )[0],
        leg("a")
    );
}

#[test]
fn missing_duplicate_metadata_deterministic() {
    let mut duplicate = leg("a");
    duplicate.family = "other".into();
    duplicate.tier = 0;
    let plan = vec![leg("a"), duplicate, leg("b")];
    let hs = vec![
        ("a".into(), struck(Class::Crash)),
        ("unknown".into(), struck(Class::Crash)),
    ];
    assert_eq!(family_floor(&hs, &plan, Role::Review), hs); // Missing b is healthy.
    assert_eq!(
        order_legs(&plan, &hs, Role::Review),
        vec![leg("b"), leg("a")]
    );
    assert_eq!(family_floor(&hs, &[], Role::Review), hs);
    assert!(order_legs(&[], &hs, Role::Review).is_empty());
}

#[test]
fn non_review_ok_preserves_review_fitness() {
    let mut h = struck(Class::ToolCallOnly);
    h.review_unfit = true;
    h.clear_on_ok(Role::Implement);
    assert_eq!(
        h,
        Health {
            review_unfit: true,
            ..Health::default()
        }
    );
    h.clear_on_ok(Role::Review);
    assert_eq!(h, Health::default());
}

#[test]
fn non_review_floor_ignores_review_fitness() {
    let mut h = struck(Class::ToolCallOnly);
    h.review_unfit = true;
    let out = family_floor(&[("a".into(), h.clone())], &[leg("a")], Role::Implement);
    h.state = State::Degraded;
    assert_eq!(out, vec![("a".into(), h)]);
}
