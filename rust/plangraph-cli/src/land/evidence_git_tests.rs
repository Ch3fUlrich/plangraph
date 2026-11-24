use super::build_tests::{g, Fx};
use super::evidence::*;
use super::evidence_git;
use super::evidence_tests::{evidence, facts, key, paths};

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
        "b1",
        &new_head,
        true,
    )
    .iter()
    .map(|r| serde_json::to_string(r).unwrap())
    .collect();
    std::fs::write(&rec, lines.join("\n")).unwrap();
    let rflags = [("records".to_owned(), rec.to_str().unwrap().to_owned())].into();
    assert_eq!(
        super::evidence_cli::report_cli(&rflags).unwrap(),
        ["2026-11-24 rebases 1 accepts_carried 1 ci_carried 1 re_reviews 0 fix_rounds_after_rebase 0 landed 0"]
    );
}

#[test]
fn a_void_condition_without_a_prior_accept_has_nothing_to_void_and_no_re_review_is_logged() {
    let mut e = evidence(0);
    e.accept = false;
    let d = decide(&e, &facts(key("other", &["a.txt", "b.txt"]), &[], false, 0));
    assert_eq!(d.accept, Accept::None);
    assert_eq!(d.ci, Ci::Rerun(CiRerun::Void(Void::PatchIdChanged)));
    let kinds = |rebased| -> Vec<Kind> {
        records_of(&d, "2026-11-24", "b1", "h", rebased)
            .into_iter()
            .map(|r| r.kind)
            .collect()
    };
    // Only an actual rebase is a rebase; an assessment of an unchanged head is not.
    assert_eq!(kinds(true), [Kind::Rebase]);
    assert!(kinds(false).is_empty());
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
    // The same head again (main moved, nothing rebased) carries but is not a rebase.
    let mut same = assess.clone();
    same.insert("head".into(), head.clone());
    super::evidence_cli::assess_cli(&same).unwrap();
    let rec = flags(&[
        ("records-out", log.to_str().unwrap()),
        ("day", "2026-11-25"),
        ("batch", "b1"),
        ("head", "h9"),
        ("kind", "fix-round-after-rebase"),
    ]);
    super::evidence_cli::record_cli(&rec).unwrap();
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
    let k = |h: &str| evidence_git::key_of(fx.path(), &base, h).unwrap();
    assert_ne!(k(&a).patch_id, k(&b).patch_id);
    // The same patch rebased over an unrelated main change keeps its identity.
    let main1 = advance_main(&fx, "log.txt", "entry 1\n");
    let moved = rebased(&fx, &a, &main1);
    assert_eq!(
        evidence_git::key_of(fx.path(), &main1, &moved).unwrap(),
        k(&a)
    );
}

#[test]
fn a_verified_carry_is_persisted_and_the_builder_admits_the_receipt_of_the_old_head() {
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
    let carry = dir.path().join("carry.json");
    let assess = flags(&[
        ("repo", fx.path().to_str().unwrap()),
        ("evidence", ev.to_str().unwrap()),
        ("main", &main1),
        ("head", &new_head),
        ("trains-now", "1"),
        ("carry-out", carry.to_str().unwrap()),
    ]);
    super::evidence_cli::assess_cli(&assess).unwrap();
    let carries: Vec<super::train::Carry> =
        vec![serde_json::from_slice(&std::fs::read(&carry).unwrap()).unwrap()];
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
    // Without the carry the receipt of the old head is refused; with it, only for exactly these heads.
    let mut parts = vec![part(&head)];
    assert!(parts[0].check_receipt().is_err());
    super::evidence_cli::attach_carries(&mut parts, &carries);
    assert!(parts[0].check_receipt().is_ok());
    let mut other = vec![part(&base)];
    super::evidence_cli::attach_carries(&mut other, &carries);
    assert!(other[0].check_receipt().is_err());
    // A void decision writes no carry.
    let mut void = assess.clone();
    std::fs::remove_file(&carry).unwrap();
    void.insert("head".into(), base.clone());
    let _ = super::evidence_cli::assess_cli(&void);
    assert!(!carry.exists());
}
