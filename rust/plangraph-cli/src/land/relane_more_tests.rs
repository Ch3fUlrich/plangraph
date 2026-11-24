use super::build_tests::g;
use super::relane::*;
use super::relane_cli::{self, ParentSpec, Spec};
use super::relane_tests::{rebased, Fx};

#[test]
fn a_lane_without_a_recorded_head_or_writer_is_refused_even_when_pushing() {
    let fx = Fx::new();
    fx.lane("lane/bare-r1", "w-bare", &[("a.txt", "bare\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    for (head, writer) in [("", "w-bare"), ("x", "")] {
        let mut l = fx.input("lane/bare-r1", &onto);
        if head.is_empty() {
            l.expected_head = head;
        } else {
            l.writer = writer;
        }
        match relane(&l).unwrap() {
            Outcome::Refused { reason, .. } => assert!(reason.contains("recorded"), "{reason}"),
            other => panic!("{}", other.line()),
        }
    }
    assert!(g(&fx.origin, &["branch", "--list", "lane/bare-r2"]).is_empty());
}

#[test]
fn fetch_and_result_refs_of_adjacent_revisions_are_disjoint() {
    let fx = Fx::new();
    let r1 = fx.lane("lane/adj-r1", "w-adj", &[("a.txt", "r1\n2\n3\n")]);
    let r2 = fx.lane("lane/adj-r2", "w-adj2", &[("b.txt", "1\nr2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let mut first = fx.input("lane/adj-r1", &onto);
    first.push = false;
    rebased(relane(&first).unwrap());
    // r1's result is the ref named for r2; r2's fetched head must not be it.
    assert!(g(&fx.work, &["for-each-ref", "refs/po/fetch/lane/adj-r2"]).is_empty());
    let mut second = fx.input("lane/adj-r2", &onto);
    second.push = false;
    let b = rebased(relane(&second).unwrap());
    assert_eq!(b.old_head, r2);
    assert_ne!(
        g(&fx.work, &["rev-parse", "refs/po/fetch/lane/adj-r2"]),
        g(&fx.work, &["rev-parse", "refs/po/result/lane/adj-r2"])
    );
    assert_eq!(g(&fx.work, &["rev-parse", "refs/po/fetch/lane/adj-r1"]), r1);
    // Both in parallel: each keeps its own commit, and r1's result (named r2) does not replace r2's fetch.
    let (a, b) = std::thread::scope(|s| {
        let (fx, onto) = (&fx, &onto);
        let run = move |branch: &str| {
            let mut l = fx.input(branch, onto);
            l.push = false;
            relane(&l).unwrap()
        };
        let a = s.spawn(move || run("lane/adj-r1"));
        let b = s.spawn(move || run("lane/adj-r2"));
        (rebased(a.join().unwrap()), rebased(b.join().unwrap()))
    });
    assert_eq!(
        (a.old_head.as_str(), b.old_head.as_str()),
        (r1.as_str(), r2.as_str())
    );
    assert_eq!(
        g(
            &fx.work,
            &["log", "--format=%s", &format!("{onto}..{}", a.new_head)]
        ),
        "edit a.txt"
    );
    assert_eq!(
        g(
            &fx.work,
            &["log", "--format=%s", &format!("{onto}..{}", b.new_head)]
        ),
        "edit b.txt"
    );
}

/// parent `lane/p-r1` (one commit) and child `lane/c-r1` on top of it, both by one writer.
fn stack(fx: &Fx) -> (String, String) {
    let p = fx.scratch("stack");
    g(&p, &["checkout", "-q", "-b", "lane/p-r1", "origin/main"]);
    std::fs::write(p.join("a.txt"), "parent\n2\n3\n").unwrap();
    g(
        &p,
        &["commit", "-q", "-am", "parent edit\n\nWriter: w-shared"],
    );
    g(&p, &["push", "-q", "origin", "lane/p-r1"]);
    let parent = g(&p, &["rev-parse", "HEAD"]);
    g(&p, &["checkout", "-q", "-b", "lane/c-r1"]);
    std::fs::write(p.join("b.txt"), "1\nchild\n3\n").unwrap();
    g(
        &p,
        &["commit", "-q", "-am", "child edit\n\nWriter: w-shared"],
    );
    g(&p, &["push", "-q", "origin", "lane/c-r1"]);
    (parent, g(&p, &["rev-parse", "HEAD"]))
}

fn stack_specs(parent: &str, child: &str) -> Vec<Spec> {
    vec![
        Spec {
            branch: "lane/p-r1".into(),
            head: parent.into(),
            writer: "w-shared".into(),
            parent: None,
        },
        Spec {
            branch: "lane/c-r1".into(),
            head: child.into(),
            writer: "w-shared".into(),
            parent: Some(ParentSpec {
                branch: "lane/p-r1".into(),
                head: parent.into(),
            }),
        },
    ]
}

#[test]
fn a_stacked_child_replays_only_its_own_commits_on_its_relaned_parent() {
    let fx = Fx::new();
    let (parent, child) = stack(&fx);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let lines = relane_cli::relane_all(
        &fx.work,
        "origin",
        &onto,
        true,
        &stack_specs(&parent, &child),
    )
    .unwrap();
    assert!(lines[1].contains("own 1->1"), "{lines:?}");
    assert_eq!(fx.log("lane/p-r2", &onto), ["parent edit"]);
    // The child sits on the parent's new head and adds only its own commit.
    assert_eq!(
        g(&fx.origin, &["rev-parse", "lane/c-r2~1"]),
        g(&fx.origin, &["rev-parse", "lane/p-r2"])
    );
    assert_eq!(fx.log("lane/c-r2", "lane/p-r2"), ["child edit"]);
}

#[test]
fn a_stacked_child_whose_parent_was_not_relaned_is_refused() {
    let fx = Fx::new();
    let (parent, child) = stack(&fx);
    let onto = fx.advance("a.txt", "main\n2\n3\n");
    let err = relane_cli::relane_all(
        &fx.work,
        "origin",
        &onto,
        true,
        &stack_specs(&parent, &child),
    )
    .unwrap_err();
    assert!(err.contains("CONFLICT lane/p-r1"), "{err}");
    assert!(
        err.contains("REFUSED lane/c-r1 its parent lane/p-r1 was not relaned"),
        "{err}"
    );
    assert!(g(&fx.origin, &["branch", "--list", "lane/c-r2"]).is_empty());
}

#[test]
fn a_merge_with_edits_of_its_own_is_refused_and_a_plain_merge_is_not() {
    let fx = Fx::new();
    let p = fx.scratch("evil");
    g(&p, &["checkout", "-q", "-b", "lane/evil-r1", "origin/main"]);
    std::fs::write(p.join("a.txt"), "lane\n2\n3\n").unwrap();
    g(&p, &["commit", "-q", "-am", "edit a.txt\n\nWriter: w-evil"]);
    fx.advance("c.txt", "1\n2\nfirst\n");
    g(&p, &["fetch", "-q", "origin"]);
    g(
        &p,
        &["merge", "-q", "--no-edit", "--no-commit", "origin/main"],
    );
    std::fs::write(p.join("b.txt"), "only in the merge\n").unwrap();
    g(&p, &["add", "-A"]);
    g(&p, &["commit", "-q", "-m", "merge main"]);
    g(&p, &["push", "-q", "origin", "lane/evil-r1"]);
    let onto = fx.advance("b.txt", "1\n2\nsecond\n");
    match relane(&fx.input("lane/evil-r1", &onto)).unwrap() {
        Outcome::Refused { reason, .. } => assert!(reason.contains("edits of its own"), "{reason}"),
        other => panic!("{}", other.line()),
    }
    assert!(g(&fx.origin, &["branch", "--list", "lane/evil-r2"]).is_empty());
}

pub(super) fn body(writer: &str) -> String {
    format!("x\\n\\nWriter: claude-sonnet-5-5 s (high writer {writer}, Claude Code, dell, D-1)")
}

#[test]
fn open_lane_prs_become_specs_parents_first_with_a_writer_that_names_the_lane() {
    let json = format!(
        r#"[
      {{"headRefName":"lane/c-lane-r1","headRefOid":"c1","baseRefName":"lane/p-lane-r1","commits":[{{"messageBody":"{c}"}},{{"messageBody":"merge main"}}]}},
      {{"headRefName":"lane/p-lane-r1","headRefOid":"p1","baseRefName":"main","commits":[{{"messageBody":"Writer: old"}},{{"messageBody":"{p}"}}]}},
      {{"headRefName":"feature/other","headRefOid":"o1","baseRefName":"main","commits":[]}},
      {{"headRefName":"lane/thief-r1","headRefOid":"t1","baseRefName":"main","commits":[{{"messageBody":"{victim}"}}]}},
      {{"headRefName":"lane/none-r1","headRefOid":"n1","baseRefName":"main","commits":[{{"messageBody":"no trailer"}}]}}
    ]"#,
        c = body("c-lane"),
        p = body("p-lane"),
        victim = body("victim-lane")
    );
    let specs = relane_cli::specs_from_prs(&json).unwrap();
    let by: std::collections::BTreeMap<_, _> = specs
        .iter()
        .map(|s| (s.branch.as_str(), s.writer.as_str()))
        .collect();
    assert_eq!(specs.len(), 4, "{by:?}");
    // The tip of the child is a merge without a trailer: its writer is read from the commit before it.
    assert!(by["lane/c-lane-r1"].contains("high writer c-lane"));
    assert!(by["lane/p-lane-r1"].contains("high writer p-lane"));
    // A lane that holds only another lane's commits, or none with a trailer, has no identity: relane refuses it.
    assert_eq!(by["lane/thief-r1"], "");
    assert_eq!(by["lane/none-r1"], "");
    let at = |b: &str| specs.iter().position(|s| s.branch == b).unwrap();
    assert!(at("lane/p-lane-r1") < at("lane/c-lane-r1"));
    assert_eq!(
        specs
            .iter()
            .find(|s| s.branch == "lane/c-lane-r1")
            .unwrap()
            .parent
            .as_ref()
            .unwrap()
            .head,
        "p1"
    );
}

#[test]
fn a_lane_holding_only_another_lanes_commits_is_refused_through_relane_open_specs() {
    let fx = Fx::new();
    let writer = "claude-sonnet-5-5 s (high writer victim-lane, Claude Code, dell, D-1)";
    let head = fx.lane("lane/thief-r1", writer, &[("a.txt", "stolen\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let json = format!(
        r#"[{{"headRefName":"lane/thief-r1","headRefOid":"{head}","baseRefName":"main","commits":[{{"messageBody":"{}"}}]}}]"#,
        body("victim-lane")
    );
    let specs = relane_cli::specs_from_prs(&json).unwrap();
    let err = relane_cli::relane_all(&fx.work, "origin", &onto, true, &specs).unwrap_err();
    assert!(err.starts_with("REFUSED lane/thief-r1"), "{err}");
    assert!(g(&fx.origin, &["branch", "--list", "lane/thief-r2"]).is_empty());
}

#[test]
fn a_second_run_after_a_push_finds_the_branch_and_reports_the_lane_done() {
    let fx = Fx::new();
    let head = fx.lane("lane/twice-r1", "w-twice", &[("a.txt", "twice\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let spec = vec![Spec {
        branch: "lane/twice-r1".into(),
        head,
        writer: "w-twice".into(),
        parent: None,
    }];
    let first = relane_cli::relane_all(&fx.work, "origin", &onto, true, &spec).unwrap();
    assert!(first[0].starts_with("REBASED lane/twice-r1"), "{first:?}");
    let pushed = g(&fx.origin, &["rev-parse", "lane/twice-r2"]);
    let second = relane_cli::relane_all(&fx.work, "origin", &onto, true, &spec).unwrap();
    assert_eq!(second, [format!("CURRENT lane/twice-r1 {pushed}")]);
    assert_eq!(g(&fx.origin, &["rev-parse", "lane/twice-r2"]), pushed);
}

#[test]
fn an_environment_failure_is_a_retry_with_its_own_exit_code() {
    let fx = Fx::new();
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let spec = fx._dir.path().join("spec.json");
    std::fs::write(
        &spec,
        r#"[{"branch":"lane/missing-r1","head":"0000000000000000000000000000000000000001","writer":"w"}]"#,
    )
    .unwrap();
    let args: Vec<String> = [
        "relane",
        "--repo",
        fx.work.to_str().unwrap(),
        "--remote",
        "origin",
        "--onto",
        &onto,
        "--spec",
        spec.to_str().unwrap(),
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(super::cli::run(std::path::Path::new("."), &args), 4);
}

#[test]
fn relane_open_needs_the_train_merge_on_main_before_it_lists_anything() {
    let fx = Fx::new();
    let lane = fx.lane("lane/off-r1", "w-off", &[("a.txt", "off\n2\n3\n")]);
    let mut flags = std::collections::BTreeMap::new();
    for (k, v) in [
        ("repo", fx.work.to_str().unwrap()),
        ("remote", "origin"),
        ("github", "o/r"),
        ("merge", lane.as_str()),
    ] {
        flags.insert(k.to_owned(), v.to_owned());
    }
    let err = relane_cli::run_open_cli(&flags).unwrap_err();
    assert!(
        err.starts_with("retry:") && err.contains("is not on main"),
        "{err}"
    );
}
