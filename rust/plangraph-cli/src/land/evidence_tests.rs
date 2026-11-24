use super::build_tests::{g, Fx};
use super::evidence::*;
use super::evidence_git;
use super::main_state::State;
use std::collections::BTreeSet;

fn paths(p: &[&str]) -> BTreeSet<String> {
    p.iter().map(|s| (*s).to_owned()).collect()
}

fn key(patch: &str, p: &[&str]) -> Key {
    Key {
        patch_id: patch.into(),
        paths: paths(p),
    }
}

fn evidence(trains_at_base: u64) -> Evidence {
    Evidence {
        key: key("pid1", &["a.txt", "b.txt"]),
        base: "base0".into(),
        head: "head0".into(),
        trains_at_base,
        accept: true,
        ci_green: true,
    }
}

fn facts(k: Key, main_paths: &[&str], conflict: bool, trains_now: u64) -> Facts {
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
    let mut recs = records_of(&carried, "2026-11-24");
    recs.extend(records_of(&void, "2026-11-24"));
    recs.push(Record::new("2026-11-25", Kind::FixRoundAfterRebase));
    let days = daily(&recs).unwrap();
    let d24 = &days["2026-11-24"];
    assert_eq!(d24.rebases, 2);
    assert_eq!(d24.accepts_carried, 1);
    assert_eq!(d24.ci_carried, 1);
    assert_eq!(d24.re_reviews, 1);
    assert_eq!(days["2026-11-25"].fix_rounds_after_rebase, 1);
    let bad = [Record::new(
        "2026-11-24",
        Kind::AcceptCarried {
            old_head: "".into(),
            new_head: "h".into(),
        },
    )];
    assert!(daily(&bad).is_err());
}

// Git-backed: the facts come from the repository.

fn assess(fx: &Fx, old: &Evidence, main: &str, head: &str, now: u64) -> Decision {
    evidence_git::assess(fx.path(), old, main, head, now).unwrap()
}

fn recorded(fx: &Fx, main: &str, head: &str) -> Evidence {
    Evidence {
        key: evidence_git::key_of(fx.path(), main, head).unwrap(),
        base: main.into(),
        head: head.into(),
        trains_at_base: 0,
        accept: true,
        ci_green: true,
    }
}

/// `head` replayed on `onto` with `cherry-pick`: the same patch on a newer main.
fn rebased(fx: &Fx, head: &str, onto: &str) -> String {
    let p = fx.path();
    g(p, &["checkout", "-q", "--detach", onto]);
    g(p, &["cherry-pick", head]);
    let new = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    new
}

fn advance_main(fx: &Fx, file: &str, content: &str) -> String {
    let new = fx.commit(&fx.base(), file, content);
    g(fx.path(), &["update-ref", "refs/heads/main", &new]);
    g(fx.path(), &["reset", "-q", "--hard", "main"]);
    new
}

#[test]
fn a_clean_rebase_over_an_unrelated_main_delta_carries() {
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let old = recorded(&fx, &base, &head);
    let main1 = advance_main(&fx, "log.txt", "entry 1\n");
    let new_head = rebased(&fx, &head, &main1);
    assert_ne!(new_head, head);
    let d = assess(&fx, &old, &main1, &new_head, 1);
    assert_eq!(
        d.accept,
        Accept::Carried {
            old_head: head,
            new_head
        }
    );
    assert!(matches!(d.ci, Ci::Carried { .. }));
}

#[test]
fn a_main_delta_on_a_part_path_voids_through_git() {
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(
        &base,
        "shared.txt",
        "line 1\nline 2\nline 3\nline 4\nline 5\nchanged\n",
    );
    let old = recorded(&fx, &base, &head);
    let main1 = advance_main(
        &fx,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    let new_head = rebased(&fx, &head, &main1);
    let d = assess(&fx, &old, &main1, &new_head, 1);
    assert_eq!(
        d.accept,
        Accept::Void {
            reason: Void::MainTouched(paths(&["shared.txt"])),
            re_review: ReReview::Full
        }
    );
}

#[test]
fn main_moving_does_not_force_a_rebase_only_a_textual_conflict_does() {
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let main1 = advance_main(&fx, "log.txt", "entry 1\n");
    assert!(!evidence_git::rebase_needed(fx.path(), &main1, &head).unwrap());
    let other = fx.commit(
        &base,
        "shared.txt",
        "mine\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    let main2 = advance_main(
        &fx,
        "shared.txt",
        "theirs\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    assert!(evidence_git::rebase_needed(fx.path(), &main2, &other).unwrap());
}

#[test]
fn the_patch_id_is_stable_across_a_clean_rebase_and_differs_for_another_patch() {
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let main1 = advance_main(&fx, "log.txt", "entry 1\n");
    let new_head = rebased(&fx, &head, &main1);
    let k = |m: &str, h: &str| evidence_git::key_of(fx.path(), m, h).unwrap();
    assert_eq!(k(&base, &head), k(&main1, &new_head));
    let other = fx.commit(&base, "a.txt", "different\n");
    assert_ne!(k(&base, &head).patch_id, k(&base, &other).patch_id);
    assert_eq!(k(&base, &head).paths, paths(&["a.txt"]));
}

#[test]
fn the_cli_prints_the_verdicts_naming_both_heads_and_the_daily_report() {
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let main1 = advance_main(&fx, "log.txt", "entry 1\n");
    let new_head = rebased(&fx, &head, &main1);
    let dir = tempfile::tempdir().unwrap();
    let ev = dir.path().join("ev.json");
    std::fs::write(
        &ev,
        serde_json::to_vec(&recorded(&fx, &base, &head)).unwrap(),
    )
    .unwrap();
    let flags: std::collections::BTreeMap<String, String> = [
        ("repo", fx.path().to_str().unwrap()),
        ("evidence", ev.to_str().unwrap()),
        ("main", main1.as_str()),
        ("head", new_head.as_str()),
        ("trains-now", "1"),
    ]
    .iter()
    .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
    .collect();
    let out = super::evidence_cli::assess_cli(&flags).unwrap();
    assert_eq!(out[0], format!("ACCEPT carried {head} -> {new_head}"));
    assert_eq!(out[1], format!("CI carried {head} -> {new_head}"));
    let rec = dir.path().join("rec.jsonl");
    let lines: Vec<String> = records_of(
        &assess(&fx, &recorded(&fx, &base, &head), &main1, &new_head, 1),
        "2026-11-24",
    )
    .iter()
    .map(|r| serde_json::to_string(r).unwrap())
    .collect();
    std::fs::write(&rec, lines.join("\n")).unwrap();
    let rflags = [("records".to_owned(), rec.to_str().unwrap().to_owned())].into();
    assert_eq!(
        super::evidence_cli::report_cli(&rflags).unwrap(),
        ["2026-11-24 rebases 1 accepts_carried 1 ci_carried 1 re_reviews 0 fix_rounds_after_rebase 0"]
    );
}
