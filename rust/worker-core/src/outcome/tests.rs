use super::*;
pub(super) fn facts(role: Role, exit: i32) -> Facts {
    Facts::new(role, exit)
}
pub(super) const ROLES: [Role; 5] = [
    Role::Implement,
    Role::Review,
    Role::Orchestrate,
    Role::Research,
    Role::Session,
];

#[test]
fn orchestrate_tools_only_is_empty() {
    let mut f = facts(Role::Orchestrate, 0);
    f.tool = true;
    assert_eq!(classify(&f).class, Class::Empty);
}
#[test]
fn lone_step_start_is_empty() {
    assert_eq!(classify(&facts(Role::Orchestrate, 0)).class, Class::Empty);
}
#[test]
fn writer_unchanged_tree_after_content_is_writer_no_changes() {
    let mut f = facts(Role::Implement, 0);
    f.content = true;
    assert_eq!(
        classify(&f).class,
        Class::Neutral(Terminal::WriterNoChanges)
    );
}
#[test]
fn seat_tool_call_only_vs_no_verdict() {
    let mut f = facts(Role::Review, 0);
    f.tool = true;
    assert_eq!(classify(&f).class, Class::NoVerdict);
    f.tool_call_only = true;
    assert_eq!(classify(&f).class, Class::ToolCallOnly);
}
#[test]
fn crash_nonzero_without_marker_empty_or_after_content() {
    for delivered in [false, true] {
        let mut f = facts(Role::Implement, 1);
        f.content = delivered;
        f.tool = delivered;
        assert_eq!(classify(&f).class, Class::Crash);
    }
}
#[test]
fn killed_external_for_minus9_and_137_only_without_own_kill() {
    for exit in [-9, 137] {
        assert_eq!(
            classify(&facts(Role::Implement, exit)).class,
            Class::KilledExternal
        );
    }
}
#[test]
fn own_kill_is_never_crash_empty_or_external() {
    for (kill, class) in [
        (OwnKill::MaxS, Class::MaxTime),
        (
            OwnKill::Monitor(KillOutcome::HangNoActivity),
            Class::HangNoActivity,
        ),
        (OwnKill::Monitor(KillOutcome::HangSilent), Class::HangSilent),
        (OwnKill::Monitor(KillOutcome::NoProgress), Class::NoProgress),
    ] {
        for exit in [-9, 137, 0, 1] {
            let mut f = facts(Role::Implement, exit);
            f.own_kill = Some(kill);
            f.marker = Some(Marker::Quota);
            assert_eq!(classify(&f).class, class);
        }
    }
}
#[test]
fn terminal_wins_over_every_other_fact() {
    for t in [
        Terminal::CallerFault,
        Terminal::Cancelled,
        Terminal::Orphaned,
        Terminal::GitConfigMutated,
        Terminal::WriterNoChanges,
        Terminal::KillIncomplete,
        Terminal::ProofFailure,
    ] {
        let mut f = facts(Role::Review, -9);
        f.terminal = Some(t);
        f.own_kill = Some(OwnKill::MaxS);
        f.marker = Some(Marker::Quota);
        f.content = true;
        f.tool = true;
        f.verdict_usable = true;
        assert_eq!(classify(&f).class, Class::Neutral(t));
        assert!(Class::Neutral(t).is_neutral());
        assert_eq!(Class::Neutral(t).scope(), Scope::None);
    }
}
#[test]
fn marker_wins_over_content_tool_and_exit_zero() {
    for (marker, class) in [
        (Marker::Quota, Class::Quota),
        (Marker::RateLimit, Class::RateLimit),
        (Marker::Auth, Class::Auth),
        (Marker::ModelUnavailable, Class::ModelUnavailable),
        (Marker::ProviderUnavailable, Class::ProviderUnavailable),
    ] {
        for exit in [0, 1, -9, 137] {
            let mut f = facts(Role::Orchestrate, exit);
            f.content = true;
            f.tool = true;
            f.marker = Some(marker);
            assert_eq!(
                classify(&f).class,
                if marker == Marker::ProviderUnavailable && exit == 0 {
                    Class::Ok
                } else {
                    class
                }
            );
        }
    }
}
#[test]
fn classify_is_total_table() {
    let mut count = 0;
    for role in ROLES {
        for exit in [0, 1, -9, 137] {
            for own_kill in [
                None,
                Some(OwnKill::Monitor(KillOutcome::HangSilent)),
                Some(OwnKill::MaxS),
            ] {
                for bits in 0..64 {
                    let mut f = facts(role, exit);
                    f.own_kill = own_kill;
                    f.content = bits & 1 != 0;
                    f.tool = bits & 2 != 0;
                    f.marker = (bits & 4 != 0).then_some(Marker::Quota);
                    f.tree_changed = bits & 8 != 0;
                    f.verdict_usable = bits & 16 != 0;
                    f.tool_call_only = bits & 32 != 0;
                    let c = classify(&f).class;
                    if own_kill.is_some() || f.marker.is_some() {
                        assert!(!matches!(
                            c,
                            Class::Crash | Class::Empty | Class::KilledExternal
                        ));
                    }
                    assert!(!c.as_str().is_empty());
                    count += 1;
                }
            }
        }
    }
    assert_eq!(count, 3840);
}
