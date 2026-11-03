use super::tests::{facts, ROLES};
use super::*;

#[test]
fn ok_by_role() {
    for role in ROLES {
        let mut f = facts(role, 0);
        f.tree_changed = true;
        f.verdict_usable = true;
        f.content = true;
        assert_eq!(classify(&f).class, Class::Ok);
    }
}
#[test]
fn max_time_is_neutral_with_leg_scope() {
    assert!(Class::MaxTime.is_neutral());
    assert_eq!(Class::MaxTime.scope(), Scope::Leg);
}
#[test]
fn scope_and_neutral_flags_match_the_table() {
    for c in [
        Class::Ok,
        Class::HangNoActivity,
        Class::HangSilent,
        Class::NoProgress,
        Class::KilledExternal,
        Class::Empty,
        Class::Crash,
        Class::ModelUnavailable,
        Class::Quota,
        Class::RateLimit,
    ] {
        assert_eq!(c.scope(), Scope::Leg);
        assert!(!c.is_neutral());
        assert!(!c.as_str().is_empty());
    }
    for (c, scope) in [
        (Class::Auth, Scope::Route),
        (Class::ProviderUnavailable, Scope::Route),
        (Class::ToolCallOnly, Scope::LegReview),
        (Class::NoVerdict, Scope::LegReview),
    ] {
        assert_eq!(c.scope(), scope);
        assert!(!c.is_neutral());
    }
}
#[test]
fn reset_carried_only_by_hard_fact_classes() {
    for marker in [
        None,
        Some(Marker::Quota),
        Some(Marker::RateLimit),
        Some(Marker::Auth),
        Some(Marker::ModelUnavailable),
        Some(Marker::ProviderUnavailable),
    ] {
        for exit in [0, 1] {
            let mut f = facts(Role::Orchestrate, exit);
            f.content = true;
            f.marker = marker;
            f.reset = Some(Reset {
                reset_at: 259_300.0,
                clamped: true,
            });
            f.evidence = Some("raw evidence".into());
            let o = classify(&f);
            assert_eq!(o.reset_at, o.class.carries_reset().then_some(259_300.0));
            assert_eq!(o.reset_clamped, o.class.carries_reset());
            assert_eq!(o.evidence, f.evidence);
        }
    }
}
#[test]
fn describe_redacts_and_caps() {
    let mut f = facts(Role::Orchestrate, 1);
    assert_eq!(classify(&f).describe(), "CRASH");
    f.evidence = Some(["sk", "-", &"a".repeat(24), "\nsecond"].concat());
    assert_eq!(classify(&f).describe(), "CRASH: [REDACTED]");
    f.evidence = Some(format!("{}\nsecond", "é".repeat(400)));
    assert_eq!(
        classify(&f).describe(),
        format!("CRASH: {}", "é".repeat(300))
    );
    f.evidence = Some(
        [
            "-----BE",
            "GIN TEST PRIVATE KEY-----\n",
            "body\n",
            "-----EN",
            "D TEST PRIVATE KEY-----",
        ]
        .concat(),
    );
    assert_eq!(classify(&f).describe(), "CRASH: [REDACTED]");
}
