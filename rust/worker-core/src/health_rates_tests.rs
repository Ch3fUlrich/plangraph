use super::*;
use crate::{
    health::{Row, State},
    health_until::{fold_with_provider, ProviderState},
    hosts::Role,
    outcome::Class,
};

fn attempt(id: usize, usable: bool) -> FirstAttempt {
    FirstAttempt {
        run_id: format!("run-{id}"),
        attempt: 1,
        route_key: "served".into(),
        model: "model".into(),
        usable,
    }
}
fn key() -> (String, String) {
    ("served".into(), "model".into())
}

#[test]
fn first_attempt_only_and_corrections() {
    let mut retry = attempt(0, true);
    retry.attempt = 2;
    let rows = vec![
        attempt(0, false),
        retry,
        attempt(0, true),
        attempt(1, false),
    ];
    let policy = RatePolicy {
        min_samples: 1,
        ..RatePolicy::default()
    };
    assert_eq!(usable_rates(&rows, policy).unwrap()[&key()], (2, 1, false));
    let mut corrected = rows;
    corrected.push(attempt(0, false));
    assert_eq!(
        usable_rates(&corrected, policy).unwrap()[&key()],
        (2, 0, true)
    );
    assert!(usable_rates(&[], policy).unwrap().is_empty());
}

#[test]
fn sample_gate_19_20() {
    let mut rows: Vec<_> = (0..19).map(|id| attempt(id, false)).collect();
    assert_eq!(
        usable_rates(&rows, RatePolicy::default()).unwrap()[&key()],
        (19, 0, false)
    );
    rows.push(attempt(19, false));
    assert_eq!(
        usable_rates(&rows, RatePolicy::default()).unwrap()[&key()],
        (20, 0, true)
    );
    for row in &mut rows[..10] {
        row.usable = true;
    }
    assert_eq!(
        usable_rates(&rows, RatePolicy::default()).unwrap()[&key()],
        (20, 10, false)
    );
}

#[test]
fn per_served_route_model_scope() {
    let mut other_route = attempt(0, true);
    other_route.route_key = "other".into();
    let mut other_model = attempt(0, true);
    other_model.model = "other".into();
    let rates = usable_rates(
        &[attempt(0, false), other_route, other_model],
        RatePolicy {
            min_samples: 1,
            ..RatePolicy::default()
        },
    )
    .unwrap();
    assert_eq!(rates.len(), 3);
    let leg = LegInfo {
        leg_key: "a".into(),
        family: "f".into(),
        tier: 1,
        route_key: "served".into(),
        model: "model".into(),
    };
    for state in [State::Closed, State::Open, State::HalfOpen, State::Degraded] {
        let original = Health {
            state,
            until_s: Some(200),
            reason: Some(Class::Quota),
            ..Health::default()
        };
        let mut hs = vec![
            ("a".into(), original.clone()),
            ("unknown".into(), Health::default()),
        ];
        let mut duplicate = leg.clone();
        duplicate.route_key = "other".into();
        apply_rates(&[leg.clone(), duplicate], &mut hs, &rates);
        let mut expected = original;
        if state == State::Closed {
            expected.state = State::Degraded;
        }
        assert_eq!(hs[0].1, expected);
        assert_eq!(hs[1].1, Health::default());
    }
}

#[test]
fn invalid_policy() {
    for policy in [
        RatePolicy {
            min_samples: 0,
            min_usable_percent: 50,
        },
        RatePolicy {
            min_samples: 20,
            min_usable_percent: 101,
        },
    ] {
        assert_eq!(usable_rates(&[], policy), Err(RateError::InvalidPolicy));
    }
    assert!(
        !usable_rates(
            &[attempt(0, false)],
            RatePolicy {
                min_samples: 1,
                min_usable_percent: 0
            }
        )
        .unwrap()[&key()]
            .2
    );
}

#[test]
fn provider_quota_window_not_midnight() {
    let row = Row {
        leg_key: "a".into(),
        route_key: "served".into(),
        ts: 100,
        role: Role::Review,
        outcome: Class::Quota,
        run_id: "run".into(),
        attempt: 1,
        duration_s: None,
        reset_at: None,
        reset_assumed: false,
        reset_clamped: false,
        kind: None,
    };
    let provider = ProviderState {
        until: Some(200_000),
        observed_s: 101,
    };
    let h = fold_with_provider("a", "served", Role::Review, &[row], 102, Some(&provider));
    assert_eq!(h.until_s, provider.until);
    assert_eq!(h.until_src, Some(crate::health::UntilSrc::OmniRoute));
    assert_eq!(h.state, State::Open);
}
