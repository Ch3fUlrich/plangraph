use super::evidence::*;
use super::main_state::State;
use std::collections::BTreeSet;

pub(super) fn paths(p: &[&str]) -> BTreeSet<String> {
    p.iter().map(|s| (*s).to_owned()).collect()
}

pub(super) fn key(patch: &str, p: &[&str]) -> Key {
    Key {
        patch_id: patch.into(),
        paths: paths(p),
    }
}

pub(super) fn evidence(trains_at_base: u64) -> Evidence {
    Evidence {
        key: key("pid1", &["a.txt", "b.txt"]),
        base: "base0".into(),
        head: "head0".into(),
        trains_at_base,
        accept: true,
        ci_green: true,
    }
}

pub(super) fn facts(k: Key, main_paths: &[&str], conflict: bool, trains_now: u64) -> Facts {
    Facts {
        key_now: k,
        new_base: "base1".into(),
        new_head: "head1".into(),
        main_paths: paths(main_paths),
        conflict,
        trains_now,
    }
}

#[test]
fn an_unchanged_patch_with_untouched_paths_keeps_accept_and_ci_naming_both_heads() {
    let d = decide(
        &evidence(5),
        &facts(key("pid1", &["a.txt", "b.txt"]), &["other.txt"], false, 6),
    );
    let carried = Accept::Carried {
        old_head: "head0".into(),
        new_head: "head1".into(),
    };
    assert_eq!(d.accept, carried);
    assert_eq!(
        d.ci,
        Ci::Carried {
            old_head: "head0".into(),
            new_head: "head1".into()
        }
    );
}

#[test]
fn a_main_delta_touching_a_part_path_voids_both() {
    let d = decide(
        &evidence(5),
        &facts(key("pid1", &["a.txt", "b.txt"]), &["b.txt", "z"], false, 5),
    );
    let void = Void::MainTouched(paths(&["b.txt"]));
    assert_eq!(
        d.accept,
        Accept::Void {
            reason: void.clone(),
            re_review: ReReview::Full
        }
    );
    assert_eq!(d.ci, Ci::Rerun(CiRerun::Void(void)));
}

#[test]
fn a_changed_patch_id_voids_both_and_re_reviews_the_range_diff() {
    let d = decide(
        &evidence(5),
        &facts(key("pid2", &["a.txt", "b.txt"]), &[], false, 5),
    );
    assert_eq!(
        d.accept,
        Accept::Void {
            reason: Void::PatchIdChanged,
            re_review: ReReview::RangeDiff {
                old_head: "head0".into(),
                new_head: "head1".into()
            }
        }
    );
    assert!(matches!(
        d.ci,
        Ci::Rerun(CiRerun::Void(Void::PatchIdChanged))
    ));
}

#[test]
fn a_changed_path_set_voids() {
    let d = decide(
        &evidence(5),
        &facts(key("pid1", &["a.txt", "c.txt"]), &[], false, 5),
    );
    assert!(matches!(
        d.accept,
        Accept::Void {
            reason: Void::PathsChanged,
            ..
        }
    ));
}

#[test]
fn a_textual_conflict_voids_and_only_the_resolution_range_diff_is_re_reviewed() {
    let d = decide(
        &evidence(5),
        &facts(key("pid1", &["a.txt", "b.txt"]), &["a.txt"], true, 5),
    );
    assert_eq!(
        d.accept,
        Accept::Void {
            reason: Void::Conflict,
            re_review: ReReview::RangeDiff {
                old_head: "head0".into(),
                new_head: "head1".into()
            }
        }
    );
}

#[test]
fn past_the_train_bound_the_ci_reruns_but_the_accept_carries() {
    let f = |now| {
        decide(
            &evidence(5),
            &facts(key("pid1", &["a.txt", "b.txt"]), &[], false, now),
        )
    };
    assert!(matches!(f(5 + TRAIN_BOUND).ci, Ci::Carried { .. }));
    let past = f(5 + TRAIN_BOUND + 1);
    assert!(matches!(past.accept, Accept::Carried { .. }));
    assert_eq!(
        past.ci,
        Ci::Rerun(CiRerun::TrainBound {
            landed: TRAIN_BOUND + 1,
            bound: TRAIN_BOUND
        })
    );
}

#[test]
fn nothing_carries_that_was_never_accepted_or_green() {
    let mut e = evidence(0);
    e.accept = false;
    e.ci_green = false;
    let d = decide(&e, &facts(e.key.clone(), &[], false, 0));
    assert_eq!(d.accept, Accept::None);
    assert_eq!(d.ci, Ci::Rerun(CiRerun::NoPassingCi));
}

#[test]
fn a_flake_does_not_void_an_accept_but_a_genuine_red_does() {
    let red = |failing: &[&str], without: &[&str], hung: &[&str]| Red {
        failing: paths(failing),
        failing_without_patch: paths(without),
        hung: paths(hung),
    };
    assert_eq!(
        classify_red(&red(&["t1", "t2"], &["t1"], &["t2"])),
        RedKind::Flake
    );
    assert_eq!(
        classify_red(&red(&["t1", "t3"], &["t1"], &[])),
        RedKind::Genuine
    );
    assert_eq!(classify_red(&red(&[], &[], &[])), RedKind::Genuine);
    // A hung test with no other failure is a flake too.
    assert_eq!(classify_red(&red(&[], &[], &["t9"])), RedKind::Flake);
    assert!(!voids_accept(RedKind::Flake));
    assert!(voids_accept(RedKind::Genuine));
}

#[test]
fn the_flake_verdict_never_decides_what_may_merge() {
    // A flake-classified red is still a red required CI: the merge gate takes no flake input.
    assert_eq!(
        may_merge(false, &State::Green),
        Err("required_ci_not_green")
    );
    assert_eq!(may_merge(true, &State::Pending), Err("main_pending"));
    assert_eq!(may_merge(true, &State::Green), Ok(()));
}

#[test]
fn the_report_counts_per_day_and_every_carry_names_old_and_new_head() {
    let carried = decide(
        &evidence(1),
        &facts(key("pid1", &["a.txt", "b.txt"]), &[], false, 2),
    );
    let void = decide(&evidence(1), &facts(key("x", &[]), &[], false, 2));
    let mut recs = records_of(&carried, "2026-11-24", "b1", "head1", true);
    recs.extend(records_of(&void, "2026-11-24", "b2", "head2", true));
    recs.push(Record::new(
        "2026-11-25",
        "b1",
        "head1",
        Kind::FixRoundAfterRebase,
    ));
    let days = daily(&recs).unwrap();
    let d24 = &days["2026-11-24"];
    assert_eq!(d24.rebases, 2);
    assert_eq!(d24.accepts_carried, 1);
    assert_eq!(d24.ci_carried, 1);
    assert_eq!(d24.re_reviews, 1);
    assert_eq!(days["2026-11-25"].fix_rounds_after_rebase, 1);
    let bad = [Record::new(
        "2026-11-24",
        "b1",
        "h",
        Kind::AcceptCarried {
            old_head: "".into(),
            new_head: "h".into(),
        },
    )];
    assert!(daily(&bad).is_err());
}

#[test]
fn the_same_event_logged_twice_counts_once_and_landed_batches_are_the_denominator() {
    let r = |kind| Record::new("2026-11-24", "b1", "h1", kind);
    let mut recs = vec![
        r(Kind::Rebase),
        r(Kind::Rebase),
        r(Kind::Landed),
        r(Kind::Landed),
    ];
    recs.push(Record::new("2026-11-24", "b2", "h2", Kind::Rebase));
    recs.push(Record::new("2026-11-24", "b2", "h2", Kind::Landed));
    let d = &daily(&recs).unwrap()["2026-11-24"];
    assert_eq!((d.rebases, d.landed), (2, 2));
    assert!(daily(&[Record::new("2026-11-24", "", "h", Kind::Landed)]).is_err());
}
