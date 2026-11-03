use super::tests::{facts, ROLES};
use super::*;

#[test]
fn ok_by_role() {
    for role in ROLES {
        for fact in 0..3 {
            let mut f = facts(role, 0);
            f.tree_changed = fact == 0;
            f.verdict_usable = fact == 1;
            f.content = fact == 2;
            let own_fact = match role {
                Role::Implement => 0,
                Role::Review => 1,
                Role::Orchestrate | Role::Research | Role::Session => 2,
            };
            let expected = if fact == own_fact {
                Class::Ok
            } else if fact != 2 {
                Class::Empty
            } else if role == Role::Implement {
                Class::Neutral(Terminal::WriterNoChanges)
            } else {
                Class::NoVerdict
            };
            assert_eq!(usable(&f), fact == own_fact, "{role:?}, fact {fact}");
            assert_eq!(classify(&f).class, expected, "{role:?}, fact {fact}");
        }
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
fn carries_reset_matches_literal_class_set() {
    for class in [
        Class::Ok,
        Class::HangNoActivity,
        Class::HangSilent,
        Class::NoProgress,
        Class::MaxTime,
        Class::KilledExternal,
        Class::Empty,
        Class::Crash,
        Class::ToolCallOnly,
        Class::NoVerdict,
        Class::ModelUnavailable,
        Class::ProviderUnavailable,
        Class::Quota,
        Class::RateLimit,
        Class::Auth,
        Class::Neutral(Terminal::CallerFault),
        Class::Neutral(Terminal::Cancelled),
        Class::Neutral(Terminal::Orphaned),
        Class::Neutral(Terminal::GitConfigMutated),
        Class::Neutral(Terminal::WriterNoChanges),
        Class::Neutral(Terminal::KillIncomplete),
        Class::Neutral(Terminal::ProofFailure),
    ] {
        assert_eq!(
            class.carries_reset(),
            expected_carries_reset(class),
            "{class:?}"
        );
    }
}

fn expected_carries_reset(class: Class) -> bool {
    [
        Class::Quota,
        Class::RateLimit,
        Class::Auth,
        Class::ModelUnavailable,
        Class::ProviderUnavailable,
    ]
    .contains(&class)
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
            assert_eq!(
                o.reset_at,
                expected_carries_reset(o.class).then_some(259_300.0)
            );
            assert_eq!(o.reset_clamped, expected_carries_reset(o.class));
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

#[test]
fn class_names_match_the_table() {
    for (class, name) in [
        (Class::Ok, "OK"),
        (Class::HangNoActivity, "HANG_NO_ACTIVITY"),
        (Class::HangSilent, "HANG_SILENT"),
        (Class::NoProgress, "NO_PROGRESS"),
        (Class::MaxTime, "MAX_TIME"),
        (Class::KilledExternal, "KILLED_EXTERNAL"),
        (Class::Empty, "EMPTY"),
        (Class::Crash, "CRASH"),
        (Class::ToolCallOnly, "TOOL_CALL_ONLY"),
        (Class::NoVerdict, "NO_VERDICT"),
        (Class::ModelUnavailable, "MODEL_UNAVAILABLE"),
        (Class::ProviderUnavailable, "PROVIDER_UNAVAILABLE"),
        (Class::Quota, "QUOTA"),
        (Class::RateLimit, "RATE_LIMIT"),
        (Class::Auth, "AUTH"),
        (Class::Neutral(Terminal::CallerFault), "caller_fault"),
        (Class::Neutral(Terminal::Cancelled), "cancelled"),
        (Class::Neutral(Terminal::Orphaned), "orphaned"),
        (
            Class::Neutral(Terminal::GitConfigMutated),
            "git_config_mutated",
        ),
        (
            Class::Neutral(Terminal::WriterNoChanges),
            "writer_no_changes",
        ),
        (Class::Neutral(Terminal::KillIncomplete), "kill_incomplete"),
        (Class::Neutral(Terminal::ProofFailure), "proof_failure"),
    ] {
        assert_eq!(class.as_str(), name, "{class:?}");
    }
}

#[test]
fn describe_redacts_secret_spanning_char_cut() {
    let prefix = "é".repeat(290) + " ";
    let secret = ["sk", "-", &"z".repeat(24)].concat();
    let mut f = facts(Role::Orchestrate, 1);
    f.evidence = Some(format!("{prefix}{secret}"));
    let description = classify(&f).describe();
    let expected: String = format!("{prefix}[REDACTED]").chars().take(300).collect();
    assert_eq!(description, format!("CRASH: {expected}"));
    assert!(!description.contains(&["sk", "-"].concat()));
    assert!(!description.contains('z'));
}

#[test]
fn non_finite_resets_are_copied_raw() {
    for reset_at in [f64::NAN, f64::INFINITY] {
        for clamped in [false, true] {
            let mut f = facts(Role::Orchestrate, 1);
            f.marker = Some(Marker::RateLimit);
            f.reset = Some(Reset { reset_at, clamped });
            let outcome = classify(&f);
            assert_eq!(outcome.class, Class::RateLimit);
            let actual = outcome.reset_at.expect("reset is carried");
            if reset_at.is_nan() {
                assert!(actual.is_nan());
            } else {
                assert_eq!(actual, f64::INFINITY);
            }
            assert_eq!(outcome.reset_clamped, clamped);
        }
    }
}
