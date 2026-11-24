use super::build_tests::g;
use super::relane_cli::{self, Spec};
use super::relane_more_tests::body;
use super::relane_tests::Fx;

pub(super) fn spec(branch: &str, head: String, writer: &str) -> Spec {
    Spec {
        branch: branch.into(),
        head,
        writer: writer.into(),
        parent: None,
    }
}

#[test]
fn a_successor_that_predates_the_new_main_is_passed_over_not_overwritten() {
    let fx = Fx::new();
    let head = fx.lane("lane/clash-r1", "w-c", &[("a.txt", "c\n2\n3\n")]);
    fx.lane("lane/clash-r2", "w-other", &[("b.txt", "1\nx\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let before = g(&fx.origin, &["rev-parse", "lane/clash-r2"]);
    let lines = relane_cli::relane_all(
        &fx.work,
        "origin",
        &onto,
        true,
        &[spec("lane/clash-r1", head, "w-c")],
    )
    .unwrap();
    assert!(lines[0].contains("pushed lane/clash-r3"), "{lines:?}");
    assert_eq!(g(&fx.origin, &["rev-parse", "lane/clash-r2"]), before);
    assert_eq!(fx.log("lane/clash-r3", &onto), ["edit a.txt"]);
}

#[test]
fn a_successor_pushed_before_main_moved_again_is_relaned_onto_the_new_tip() {
    let fx = Fx::new();
    let head = fx.lane("lane/again-r1", "w-a", &[("a.txt", "a\n2\n3\n")]);
    let first = fx.advance("c.txt", "1\n2\nfirst\n");
    let l = spec("lane/again-r1", head, "w-a");
    relane_cli::relane_all(&fx.work, "origin", &first, true, std::slice::from_ref(&l)).unwrap();
    let second = fx.advance("b.txt", "1\n2\nsecond\n");
    let lines = relane_cli::relane_all(&fx.work, "origin", &second, true, &[l]).unwrap();
    assert!(
        lines[0].starts_with("REBASED lane/again-r1") && lines[0].contains("pushed lane/again-r3"),
        "{lines:?}"
    );
    assert_eq!(fx.log("lane/again-r3", &second), ["edit a.txt"]);
}

#[test]
fn a_successor_with_no_own_commit_or_other_patches_is_not_adopted() {
    let fx = Fx::new();
    let head = fx.lane("lane/hollow-r1", "w-h", &[("a.txt", "h\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    // A successor that is main itself: no own commit.
    g(&fx.origin, &["branch", "lane/hollow-r2", &onto]);
    let l = spec("lane/hollow-r1", head.clone(), "w-h");
    let err = relane_cli::relane_all(&fx.work, "origin", &onto, true, std::slice::from_ref(&l))
        .unwrap_err();
    assert!(
        err.contains("REFUSED lane/hollow-r1 lane/hollow-r2 is not this lane's result"),
        "{err}"
    );
    // A successor on main with the same writer but another patch.
    let other = fx.lane("lane/other-r1", "w-o", &[("b.txt", "1\nother\n3\n")]);
    let onto = fx.advance("d.txt", "main again\n");
    let p = fx.scratch("rival");
    g(
        &p,
        &["checkout", "-q", "-b", "lane/other-r2", "origin/main"],
    );
    std::fs::write(p.join("b.txt"), "1\nrival\n3\n").unwrap();
    g(&p, &["commit", "-q", "-am", "rival\n\nWriter: w-o"]);
    g(&p, &["push", "-q", "origin", "lane/other-r2"]);
    let rival = g(&fx.origin, &["rev-parse", "lane/other-r2"]);
    let err = relane_cli::relane_all(
        &fx.work,
        "origin",
        &onto,
        true,
        &[spec("lane/other-r1", other, "w-o")],
    )
    .unwrap_err();
    assert!(
        err.contains("lane/other-r2 is not this lane's result"),
        "{err}"
    );
    assert_eq!(g(&fx.origin, &["rev-parse", "lane/other-r2"]), rival);
}

#[test]
fn a_lane_name_inside_a_longer_one_is_not_the_lane() {
    let json = format!(
        r#"[
      {{"headRefName":"lane/alpha-r1","headRefOid":"a1","baseRefName":"main","commits":[{{"messageBody":"{extra}"}}]}},
      {{"headRefName":"lane/beta-r1","headRefOid":"b1","baseRefName":"main","commits":[{{"messageBody":"{pre}"}}]}},
      {{"headRefName":"lane/dell-r1","headRefOid":"d1","baseRefName":"main","commits":[{{"messageBody":"{host}"}}]}},
      {{"headRefName":"lane/gamma-r1","headRefOid":"c1","baseRefName":"main","commits":[{{"messageBody":"{ok}"}}]}}
    ]"#,
        extra = body("alpha-extra"),
        pre = body("pre-beta"),
        ok = body("gamma"),
        host = body("victim-lane")
    );
    let specs = relane_cli::specs_from_prs(&json).unwrap();
    let by: std::collections::BTreeMap<_, _> = specs
        .iter()
        .map(|s| (s.branch.as_str(), s.writer.as_str()))
        .collect();
    assert_eq!(by["lane/alpha-r1"], "");
    assert_eq!(by["lane/beta-r1"], "");
    // `dell` is the host field of this Writer, not its lane.
    assert_eq!(by["lane/dell-r1"], "");
    assert!(by["lane/gamma-r1"].contains("writer gamma,"));
}

#[test]
fn a_pr_list_that_cannot_be_decoded_is_a_retry_not_a_final_refusal() {
    let err = relane_cli::specs_from_prs("<html>rate limited</html>").unwrap_err();
    assert!(err.starts_with("retry:"), "{err}");
}

#[test]
fn a_successor_with_the_same_patch_id_but_other_content_is_not_adopted() {
    let fx = Fx::new();
    let head = fx.lane("lane/ws-r1", "w-ws", &[("a.txt", "ws\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let p = fx.scratch("ws-rival");
    g(&p, &["checkout", "-q", "-b", "lane/ws-r2", "origin/main"]);
    // Whitespace only: `patch-id --stable` gives the same id for both patches.
    std::fs::write(p.join("a.txt"), "ws \n2\n3\n").unwrap();
    g(&p, &["commit", "-q", "-am", "edit a.txt\n\nWriter: w-ws"]);
    g(&p, &["push", "-q", "origin", "lane/ws-r2"]);
    let err = relane_cli::relane_all(
        &fx.work,
        "origin",
        &onto,
        true,
        &[spec("lane/ws-r1", head, "w-ws")],
    )
    .unwrap_err();
    assert!(
        err.contains("lane/ws-r2 is not this lane's result"),
        "{err}"
    );
}

#[test]
fn a_successor_with_extra_history_is_not_adopted() {
    let fx = Fx::new();
    let head = fx.lane("lane/extra-r1", "w-e", &[("a.txt", "e\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let l = spec("lane/extra-r1", head, "w-e");
    relane_cli::relane_all(&fx.work, "origin", &onto, true, std::slice::from_ref(&l)).unwrap();
    let p = fx.scratch("extra-more");
    g(&p, &["checkout", "-q", "lane/extra-r2"]);
    std::fs::write(p.join("b.txt"), "1\nmore\n3\n").unwrap();
    g(&p, &["commit", "-q", "-am", "more\n\nWriter: w-e"]);
    g(&p, &["push", "-q", "origin", "lane/extra-r2"]);
    let err = relane_cli::relane_all(&fx.work, "origin", &onto, true, &[l]).unwrap_err();
    assert!(
        err.contains("lane/extra-r2 is not this lane's result"),
        "{err}"
    );
}

#[test]
fn at_the_scan_limit_the_last_free_name_is_pushed_and_then_adopted_on_retry() {
    let fx = Fx::new();
    let head = fx.lane("lane/lim-r1", "w-l", &[("a.txt", "l\n2\n3\n")]);
    let stale = g(&fx.origin, &["rev-parse", "main"]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let l = spec("lane/lim-r1", head, "w-l");
    for n in 2..=20 {
        g(&fx.origin, &["branch", &format!("lane/lim-r{n}"), &stale]);
    }
    // r2..r20 are stale: r21 is the twentieth and last candidate.
    let first =
        relane_cli::relane_all(&fx.work, "origin", &onto, true, std::slice::from_ref(&l)).unwrap();
    assert!(first[0].contains("pushed lane/lim-r21"), "{first:?}");
    let retry =
        relane_cli::relane_all(&fx.work, "origin", &onto, true, std::slice::from_ref(&l)).unwrap();
    assert!(
        retry[0].starts_with("CURRENT lane/lim-r1 ") && retry[0].contains("pushed lane/lim-r21"),
        "{retry:?}"
    );
    // With every candidate stale there is no name to push to, and the retry says the same.
    let fx = Fx::new();
    let head = fx.lane("lane/full-r1", "w-f", &[("a.txt", "f\n2\n3\n")]);
    let stale = g(&fx.origin, &["rev-parse", "main"]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    for n in 2..=21 {
        g(&fx.origin, &["branch", &format!("lane/full-r{n}"), &stale]);
    }
    let l = spec("lane/full-r1", head, "w-f");
    for _ in 0..2 {
        let err = relane_cli::relane_all(&fx.work, "origin", &onto, true, std::slice::from_ref(&l))
            .unwrap_err();
        assert!(err.contains("no free successor name"), "{err}");
    }
    assert!(g(&fx.origin, &["branch", "--list", "lane/full-r22"]).is_empty());
}

#[test]
fn a_successor_with_the_same_final_tree_but_other_steps_is_not_adopted() {
    let fx = Fx::new();
    let head = fx.lane(
        "lane/steps-r1",
        "w-s",
        &[("a.txt", "s\n2\n3\n"), ("b.txt", "1\ns\n3\n")],
    );
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let p = fx.scratch("steps-rival");
    g(
        &p,
        &["checkout", "-q", "-b", "lane/steps-r2", "origin/main"],
    );
    // The same two edits, in the other order: the tip tree and the commit count are the fresh result's.
    for (f, c) in [("b.txt", "1\ns\n3\n"), ("a.txt", "s\n2\n3\n")] {
        std::fs::write(p.join(f), c).unwrap();
        g(&p, &["add", "-A"]);
        g(
            &p,
            &["commit", "-q", "-m", &format!("edit {f}\n\nWriter: w-s")],
        );
    }
    g(&p, &["push", "-q", "origin", "lane/steps-r2"]);
    let fresh = g(&fx.origin, &["rev-parse", "lane/steps-r2^{tree}"]);
    let err = relane_cli::relane_all(
        &fx.work,
        "origin",
        &onto,
        true,
        &[spec("lane/steps-r1", head, "w-s")],
    )
    .unwrap_err();
    assert!(
        err.contains("lane/steps-r2 is not this lane's result"),
        "{err}"
    );
    assert!(!fresh.is_empty());
}

#[test]
fn the_record_file_gets_a_line_per_lane_also_when_a_retry_adopts_the_successor() {
    let fx = Fx::new();
    let head = fx.lane(
        "lane/rec-r1",
        "w-r",
        &[("a.txt", "dup\n2\n3\n"), ("b.txt", "1\nrest\n3\n")],
    );
    // The first edit is already on main: dropped by the rebase, so the patch ids differ.
    let onto = fx.advance("a.txt", "dup\n2\n3\n");
    let record = fx._dir.path().join("relane.jsonl");
    let l = [spec("lane/rec-r1", head.clone(), "w-r")];
    for _ in 0..2 {
        relane_cli::relane_all_with(&fx.work, "origin", &onto, true, &l, Some(&record)).unwrap();
    }
    let lines: Vec<serde_json::Value> = std::fs::read_to_string(&record)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(lines.len(), 2);
    let pushed = g(&fx.origin, &["rev-parse", "lane/rec-r2"]);
    for (line, kind) in lines.iter().zip(["REBASED", "CURRENT"]) {
        assert_eq!(line["kind"], kind);
        assert_eq!(line["old_head"], head.as_str());
        assert_eq!(line["new_head"], pushed.as_str());
        assert_eq!(line["pushed"], "lane/rec-r2");
        assert_eq!(line["onto"], onto.as_str());
        // One patch was dropped: the adopted head reports its own inequality, not the fresh result's.
        assert_eq!(line["patch_ids_equal"], false);
        assert_eq!(
            (line["own_before"].as_u64(), line["own_after"].as_u64()),
            (Some(2), Some(1))
        );
    }
}

#[test]
fn a_second_writer_phrase_later_in_the_note_is_not_the_lane() {
    let note = "x\\n\\nWriter: claude-sonnet-5-5 s (high writer victim-lane, Claude Code, dell, D-1, previous writer replacement)";
    let json = format!(
        r#"[
      {{"headRefName":"lane/replacement-r1","headRefOid":"r1","baseRefName":"main","commits":[{{"messageBody":"{note}"}}]}},
      {{"headRefName":"lane/victim-lane-r1","headRefOid":"v1","baseRefName":"main","commits":[{{"messageBody":"{note}"}}]}}
    ]"#
    );
    let specs = relane_cli::specs_from_prs(&json).unwrap();
    let by: std::collections::BTreeMap<_, _> = specs
        .iter()
        .map(|s| (s.branch.as_str(), s.writer.as_str()))
        .collect();
    assert_eq!(by["lane/replacement-r1"], "");
    assert!(by["lane/victim-lane-r1"].contains("previous writer replacement"));
}
