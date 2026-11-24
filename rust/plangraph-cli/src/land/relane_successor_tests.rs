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
        err.contains("REFUSED lane/hollow-r1 lane/hollow-r2 holds none"),
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
    assert!(err.contains("lane/other-r2 holds none"), "{err}");
    assert_eq!(g(&fx.origin, &["rev-parse", "lane/other-r2"]), rival);
}

#[test]
fn a_lane_name_inside_a_longer_one_is_not_the_lane() {
    let json = format!(
        r#"[
      {{"headRefName":"lane/alpha-r1","headRefOid":"a1","baseRefName":"main","commits":[{{"messageBody":"{extra}"}}]}},
      {{"headRefName":"lane/beta-r1","headRefOid":"b1","baseRefName":"main","commits":[{{"messageBody":"{pre}"}}]}},
      {{"headRefName":"lane/gamma-r1","headRefOid":"c1","baseRefName":"main","commits":[{{"messageBody":"{ok}"}}]}}
    ]"#,
        extra = body("alpha-extra"),
        pre = body("pre-beta"),
        ok = body("gamma")
    );
    let specs = relane_cli::specs_from_prs(&json).unwrap();
    let by: std::collections::BTreeMap<_, _> = specs
        .iter()
        .map(|s| (s.branch.as_str(), s.writer.as_str()))
        .collect();
    assert_eq!(by["lane/alpha-r1"], "");
    assert_eq!(by["lane/beta-r1"], "");
    assert!(by["lane/gamma-r1"].contains("writer gamma,"));
}

#[test]
fn a_pr_list_that_cannot_be_decoded_is_a_retry_not_a_final_refusal() {
    let err = relane_cli::specs_from_prs("<html>rate limited</html>").unwrap_err();
    assert!(err.starts_with("retry:"), "{err}");
}
