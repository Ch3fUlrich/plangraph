use super::build_tests::{g, Fx};
use super::evidence::*;
use super::evidence_git;
use super::evidence_tests::{evidence, facts, key, paths};

// Git-backed: the facts come from the repository.

fn assess(fx: &Fx, old: &Evidence, main: &str, head: &str, now: u64) -> Decision {
    evidence_git::assess(fx.path(), old, main, head, None, now).unwrap()
}

fn recorded(fx: &Fx, main: &str, head: &str) -> Evidence {
    Evidence {
        key: evidence_git::key_on(fx.path(), main, head, None).unwrap(),
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
    let k = |m: &str, h: &str| evidence_git::key_on(fx.path(), m, h, None).unwrap();
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
        "b1",
        &new_head,
    )
    .iter()
    .map(|r| serde_json::to_string(r).unwrap())
    .collect();
    std::fs::write(&rec, lines.join("\n")).unwrap();
    let rflags = [("records".to_owned(), rec.to_str().unwrap().to_owned())].into();
    assert_eq!(
        super::evidence_cli::report_cli(&rflags).unwrap(),
        ["2026-11-24 rebases 0 accepts_carried 1 ci_carried 1 re_reviews 0 fix_rounds_after_rebase 0 landed 0"]
    );
}

#[test]
fn a_void_condition_without_a_prior_accept_has_nothing_to_void_and_no_re_review_is_logged() {
    let mut e = evidence(0);
    e.accept = false;
    let d = decide(&e, &facts(key("other", &["a.txt", "b.txt"]), &[], false, 0));
    assert_eq!(d.accept, Accept::None);
    assert_eq!(d.ci, Ci::Rerun(CiRerun::Void(Void::PatchIdChanged)));
    // An assessment logs what it decided, never a rebase: that is logged by whoever ran it.
    let kinds: Vec<Kind> = records_of(&d, "2026-11-24", "b1", "h")
        .into_iter()
        .map(|r| r.kind)
        .collect();
    assert!(kinds.is_empty());
}

pub(super) fn flags(pairs: &[(&str, &str)]) -> std::collections::BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
        .collect()
}

#[test]
fn assess_appends_its_records_and_the_record_command_adds_a_rebase_fix_round() {
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
    let log = dir.path().join("log.jsonl");
    let assess = flags(&[
        ("repo", fx.path().to_str().unwrap()),
        ("evidence", ev.to_str().unwrap()),
        ("main", &main1),
        ("head", &new_head),
        ("trains-now", "1"),
        ("records-out", log.to_str().unwrap()),
        ("day", "2026-11-24"),
        ("batch", "b1"),
    ]);
    super::evidence_cli::assess_cli(&assess).unwrap();
    super::evidence_cli::assess_cli(&assess).unwrap();
    // The same head again (main moved) carries too. Whoever ran the rebase logs it once.
    let mut same = assess.clone();
    same.insert("head".into(), head.clone());
    super::evidence_cli::assess_cli(&same).unwrap();
    let rec = |day: &str, head: &str, kind: &str| {
        flags(&[
            ("records-out", log.to_str().unwrap()),
            ("day", day),
            ("batch", "b1"),
            ("head", head),
            ("kind", kind),
        ])
    };
    super::evidence_cli::record_cli(&rec("2026-11-24", &new_head, "rebase")).unwrap();
    super::evidence_cli::record_cli(&rec("2026-11-24", &new_head, "rebase")).unwrap();
    super::evidence_cli::record_cli(&rec("2026-11-25", "h9", "fix-round-after-rebase")).unwrap();
    assert!(super::evidence_cli::record_cli(&flags(&[
        ("records-out", "x"),
        ("day", "d"),
        ("kind", "nope")
    ]))
    .is_err());
    let out =
        super::evidence_cli::report_cli(&flags(&[("records", log.to_str().unwrap())])).unwrap();
    assert_eq!(
        out,
        [
            "2026-11-24 rebases 1 accepts_carried 2 ci_carried 2 re_reviews 0 fix_rounds_after_rebase 0 landed 0",
            "2026-11-25 rebases 0 accepts_carried 0 ci_carried 0 re_reviews 0 fix_rounds_after_rebase 1 landed 0",
        ]
    );
}

#[test]
fn the_red_command_classifies_a_flake_and_leaves_the_merge_to_ci_and_main() {
    let dir = tempfile::tempdir().unwrap();
    let red = dir.path().join("red.json");
    let write =
        |p: &std::path::Path, v: serde_json::Value| std::fs::write(p, v.to_string()).unwrap();
    write(
        &red,
        serde_json::json!({"failing": ["t1"], "failing_without_patch": ["t1"], "hung": []}),
    );
    let main = dir.path().join("main.json");
    write(&main, serde_json::json!({"state": "green"}));
    let run = |ci: &str| {
        super::evidence_cli::red_cli(&flags(&[
            ("red", red.to_str().unwrap()),
            ("ci-green", ci),
            ("main-state", main.to_str().unwrap()),
        ]))
        .unwrap()
    };
    assert_eq!(
        run("false"),
        [
            "RED flake: a carried ACCEPT stays",
            "MERGE refused required_ci_not_green"
        ]
    );
    assert_eq!(
        run("true"),
        ["RED flake: a carried ACCEPT stays", "MERGE ok"]
    );
    write(
        &red,
        serde_json::json!({"failing": ["t2"], "failing_without_patch": [], "hung": []}),
    );
    assert_eq!(run("false")[0], "RED genuine: a carried ACCEPT is void");
    write(&main, serde_json::json!({"state": "pending"}));
    assert_eq!(run("true")[1], "MERGE refused main_pending");
}

#[test]
fn a_main_commit_on_a_batch_path_that_was_reverted_still_voids() {
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let old = recorded(&fx, &base, &head);
    // Main adds a.txt and removes it again: its endpoint trees match the evidence base on that path.
    let touched = advance_main(&fx, "a.txt", "other\n");
    let p = fx.path();
    g(p, &["rm", "-q", "a.txt"]);
    g(p, &["commit", "-q", "-m", "revert a.txt"]);
    let main = g(p, &["rev-parse", "HEAD"]);
    g(p, &["update-ref", "refs/heads/main", &main]);
    g(p, &["checkout", "-q", "main"]);
    assert_ne!(touched, main);
    assert_eq!(g(p, &["diff", "--name-only", &base, &main]), "");
    let new_head = rebased(&fx, &head, &main);
    let d = assess(&fx, &old, &main, &new_head, 1);
    assert!(
        matches!(&d.accept, Accept::Void { reason: Void::MainTouched(p), .. } if p.contains("a.txt")),
        "{d:?}"
    );
}

#[test]
fn a_whitespace_only_change_to_a_string_is_a_different_patch() {
    let fx = Fx::new();
    let base = fx.base();
    let a = fx.commit(&base, "a.rs", "let s = \"ab\";\n");
    let b = fx.commit(&base, "a.rs", "let s = \"a b\";\n");
    let k = |h: &str| evidence_git::key_on(fx.path(), &base, h, None).unwrap();
    assert_ne!(k(&a).patch_id, k(&b).patch_id);
    // The same patch rebased over an unrelated main change keeps its identity.
    let main1 = advance_main(&fx, "log.txt", "entry 1\n");
    let moved = rebased(&fx, &a, &main1);
    assert_eq!(
        evidence_git::key_on(fx.path(), &main1, &moved, None).unwrap(),
        k(&a)
    );
}

#[test]
fn a_carry_is_reassessed_at_admission_and_a_superseded_one_is_cleared() {
    use super::train::{Parent, PartIn, PartReceipt};
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
    let carries = dir.path().join("carries.json");
    let assess = |main: &str, head: &str, trains: &str| {
        flags(&[
            ("repo", fx.path().to_str().unwrap()),
            ("evidence", ev.to_str().unwrap()),
            ("main", main),
            ("head", head),
            ("trains-now", trains),
            ("carry-out", carries.to_str().unwrap()),
        ])
    };
    let part = |receipt_head: &str| PartIn {
        pr: 7,
        head: new_head.clone(),
        parent: None::<Parent>,
        receipt: PartReceipt {
            run_id: "run-7".into(),
            head: receipt_head.into(),
            carried: None,
        },
    };
    let admit = |build_base: &str, trains: &str, receipt_head: &str| {
        let mut parts = vec![part(receipt_head)];
        let f = flags(&[
            ("carries", carries.to_str().unwrap()),
            ("trains-landed", trains),
        ]);
        super::evidence_cli::admit_carries(fx.path(), build_base, &f, &mut parts).unwrap();
        // The receipt is the ACCEPT; whether the part CI carried is reported next to it.
        (
            parts[0].check_receipt().is_ok(),
            parts[0].receipt.carried.as_ref().map(|c| c.ci_carried),
        )
    };
    // One format for both entry points: assess writes the file that build reads.
    super::evidence_cli::assess_cli(&assess(&main1, &new_head, "1")).unwrap();
    assert!(!part(&head).check_receipt().is_ok());
    assert_eq!(admit(&main1, "1", &head), (true, Some(true)));
    // Only for exactly the heads the carry names.
    assert_eq!(admit(&main1, "1", &base), (false, None));
    // Four trains landed since the evidence base: the ACCEPT still carries and the part CI must run
    // again; the carry says so, and the receipt is admitted for a fresh CI run to stand beside it.
    assert_eq!(admit(&main1, "4", &head), (true, Some(false)));
    super::evidence_cli::assess_cli(&assess(&main1, &new_head, "4")).unwrap();
    assert!(std::fs::read_to_string(&carries)
        .unwrap()
        .contains("\"ci_carried\":false"));
    // Main moved onto a path of the batch since the assessment: refused at the base being built.
    let main2 = advance_main(&fx, "a.txt", "other\n");
    assert_eq!(admit(&main2, "1", &head), (false, None));
    // Reassessing without a carry clears the entry the earlier assessment left.
    super::evidence_cli::assess_cli(&assess(&main2, &new_head, "1")).unwrap();
    assert_eq!(std::fs::read_to_string(&carries).unwrap(), "[]");
    assert_eq!(admit(&main1, "1", &head), (false, None));
}

#[test]
fn a_merge_is_judged_by_what_it_changed_against_main_and_a_reverted_branch_commit_still_counts() {
    let fx = Fx::new();
    let base0 = fx.base();
    let p = fx.path();
    // Main already changed shared.txt before the evidence base.
    let ev_base = advance_main(
        &fx,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    let head = fx.commit(
        &ev_base,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nmine\n",
    );
    let old = recorded(&fx, &ev_base, &head);
    // A branch from before that change adds other.txt and is merged into main.
    let side = fx.commit(&base0, "other.txt", "x\n");
    g(p, &["checkout", "-q", "--detach", &ev_base]);
    g(p, &["merge", "--no-ff", "-q", "-m", "merge side", &side]);
    let merged = g(p, &["rev-parse", "HEAD"]);
    g(p, &["update-ref", "refs/heads/main", &merged]);
    g(p, &["checkout", "-q", "main"]);
    let d = assess(&fx, &old, &merged, &head, 1);
    assert!(matches!(d.accept, Accept::Carried { .. }), "{d:?}");
    // A branch that adds and removes a path the batch touches: the merge itself changes nothing there.
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let old = recorded(&fx, &base, &head);
    let add = fx.commit(&base, "a.txt", "other\n");
    let p = fx.path();
    g(p, &["checkout", "-q", "--detach", &add]);
    g(p, &["rm", "-q", "a.txt"]);
    g(p, &["commit", "-q", "-m", "remove a.txt"]);
    let side = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "--detach", &base]);
    g(p, &["merge", "--no-ff", "-q", "-m", "merge side", &side]);
    let merged = g(p, &["rev-parse", "HEAD"]);
    g(p, &["update-ref", "refs/heads/main", &merged]);
    g(p, &["checkout", "-q", "main"]);
    let d = assess(&fx, &old, &merged, &head, 1);
    assert!(
        matches!(&d.accept, Accept::Void { reason: Void::MainTouched(t), .. } if t.contains("a.txt")),
        "{d:?}"
    );
}

#[test]
fn a_nested_merge_that_synced_main_into_a_side_branch_inherits_nothing() {
    let fx = Fx::new();
    let base0 = fx.base();
    let p = fx.path();
    let ev_base = advance_main(
        &fx,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    let head = fx.commit(
        &ev_base,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nmine\n",
    );
    let old = recorded(&fx, &ev_base, &head);
    // A side branch from before the evidence base syncs main into itself, then main merges the branch.
    let side = fx.commit(&base0, "other.txt", "x\n");
    g(p, &["checkout", "-q", "--detach", &side]);
    g(
        p,
        &[
            "merge",
            "--no-ff",
            "-q",
            "-m",
            "sync main into side",
            &ev_base,
        ],
    );
    let synced = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "--detach", &ev_base]);
    g(p, &["merge", "--no-ff", "-q", "-m", "land side", &synced]);
    let landed = g(p, &["rev-parse", "HEAD"]);
    g(p, &["update-ref", "refs/heads/main", &landed]);
    g(p, &["checkout", "-q", "main"]);
    let d = assess(&fx, &old, &landed, &head, 1);
    assert!(matches!(d.accept, Accept::Carried { .. }), "{d:?}");
}

#[test]
fn a_mainline_merge_that_takes_a_side_parents_version_of_a_file_still_counts() {
    let fx = Fx::new();
    let base0 = fx.base();
    let p = fx.path();
    let ev_base = advance_main(
        &fx,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    let head = fx.commit(
        &ev_base,
        "shared.txt",
        "head change\nline 2\nline 3\nline 4\nline 5\nmine\n",
    );
    let old = recorded(&fx, &ev_base, &head);
    // Two merges on main's own line: the first takes the old parent's tree (shared.txt goes back to
    // its earlier content), the second takes main's tree again. Neither differs from all its parents.
    let tree = |c: &str| g(p, &["rev-parse", &format!("{c}^{{tree}}")]);
    let m1 = g(
        p,
        &[
            "commit-tree",
            &tree(&base0),
            "-p",
            &ev_base,
            "-p",
            &base0,
            "-m",
            "take the old version",
        ],
    );
    let m2 = g(
        p,
        &[
            "commit-tree",
            &tree(&ev_base),
            "-p",
            &m1,
            "-p",
            &ev_base,
            "-m",
            "restore",
        ],
    );
    g(p, &["update-ref", "refs/heads/main", &m2]);
    g(p, &["reset", "-q", "--hard", "main"]);
    assert_eq!(tree(&m2), tree(&ev_base));
    let d = assess(&fx, &old, &m2, &head, 1);
    assert!(
        matches!(&d.accept, Accept::Void { reason: Void::MainTouched(t), .. } if t.contains("shared.txt")),
        "{d:?}"
    );
}

#[test]
fn a_genuine_red_voids_the_stored_accept_and_a_later_green_ci_cannot_bring_it_back() {
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
    let carries = dir.path().join("carries.json");
    let assess = flags(&[
        ("repo", fx.path().to_str().unwrap()),
        ("evidence", ev.to_str().unwrap()),
        ("main", &main1),
        ("head", &new_head),
        ("trains-now", "1"),
        ("carry-out", carries.to_str().unwrap()),
    ]);
    super::evidence_cli::assess_cli(&assess).unwrap();
    assert!(std::fs::read_to_string(&carries)
        .unwrap()
        .contains(&new_head));
    // A genuine red: the ACCEPT is void in the stored evidence and the carry of the head is dropped.
    let red = dir.path().join("red.json");
    let state = dir.path().join("main.json");
    std::fs::write(
        &red,
        r#"{"failing":["t1"],"failing_without_patch":[],"hung":[]}"#,
    )
    .unwrap();
    std::fs::write(&state, r#"{"state":"green"}"#).unwrap();
    let out = super::evidence_cli::red_cli(&flags(&[
        ("red", red.to_str().unwrap()),
        ("ci-green", "false"),
        ("main-state", state.to_str().unwrap()),
        ("evidence", ev.to_str().unwrap()),
        ("carries", carries.to_str().unwrap()),
        ("head", &new_head),
    ]))
    .unwrap();
    assert_eq!(out[0], "RED genuine: a carried ACCEPT is void");
    assert_eq!(std::fs::read_to_string(&carries).unwrap(), "[]");
    // A later green CI run of the same head: the evidence says accept false, so nothing carries.
    let mut green: Evidence = serde_json::from_slice(&std::fs::read(&ev).unwrap()).unwrap();
    assert!(!green.accept);
    green.ci_green = true;
    std::fs::write(&ev, serde_json::to_vec(&green).unwrap()).unwrap();
    let out = super::evidence_cli::assess_cli(&assess).unwrap();
    assert_eq!(out[0], "ACCEPT none");
    assert_eq!(std::fs::read_to_string(&carries).unwrap(), "[]");
    // A flake leaves everything as it was.
    std::fs::write(
        &red,
        r#"{"failing":["t1"],"failing_without_patch":["t1"],"hung":[]}"#,
    )
    .unwrap();
    green.accept = true;
    std::fs::write(&ev, serde_json::to_vec(&green).unwrap()).unwrap();
    super::evidence_cli::red_cli(&flags(&[
        ("red", red.to_str().unwrap()),
        ("ci-green", "false"),
        ("main-state", state.to_str().unwrap()),
        ("evidence", ev.to_str().unwrap()),
    ]))
    .unwrap();
    let kept: Evidence = serde_json::from_slice(&std::fs::read(&ev).unwrap()).unwrap();
    assert!(kept.accept);
}

#[test]
fn a_stacked_child_is_keyed_on_its_own_diff_so_a_parent_only_change_leaves_its_evidence() {
    let fx = Fx::new();
    let base = fx.base();
    let p = fx.path();
    // Parent P changes a.txt; child C, stacked on P, changes b.txt.
    let parent = fx.commit(&base, "a.txt", "parent\n");
    let child = fx.commit(&parent, "b.txt", "child\n");
    let old = Evidence {
        key: evidence_git::key_on(p, &base, &child, Some(&parent)).unwrap(),
        base: base.clone(),
        head: child.clone(),
        trains_at_base: 0,
        accept: true,
        ci_green: true,
    };
    // The parent is amended (its own content changes) and the child is rebased onto it.
    let parent2 = fx.commit(&base, "a.txt", "parent, amended\n");
    g(p, &["checkout", "-q", "--detach", &parent2]);
    g(p, &["cherry-pick", &child]);
    let child2 = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    let assess =
        |parent: Option<&str>| evidence_git::assess(p, &old, &base, &child2, parent, 1).unwrap();
    // Keyed on the diff against the parent: the child's patch and paths are unchanged.
    assert!(matches!(
        assess(Some(&parent2)).accept,
        Accept::Carried { .. }
    ));
    // Keyed against main, the unlanded parent's diff is part of the child's and the key moved.
    assert!(matches!(
        assess(None).accept,
        Accept::Void {
            reason: Void::PatchIdChanged,
            ..
        } | Accept::Void {
            reason: Void::PathsChanged,
            ..
        }
    ));
}
