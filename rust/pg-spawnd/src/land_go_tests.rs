use crate::land_go::{handle, parse_row, pass, GoRow, Landing};
use std::cell::RefCell;

const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";

struct Fake {
    now: String,
    behind: u64,
    checks: Result<(), String>,
    land: Result<String, String>,
    posts: RefCell<Vec<String>>,
    lands: RefCell<u32>,
}

impl Fake {
    fn green() -> Self {
        Fake {
            now: HEAD.into(),
            behind: 0,
            checks: Ok(()),
            land: Ok("c".repeat(40)),
            posts: RefCell::default(),
            lands: RefCell::default(),
        }
    }
}

impl Landing for Fake {
    fn pr_head(&self, _: u64) -> Result<String, String> {
        Ok(self.now.clone())
    }
    fn behind_by(&self, _: &str) -> Result<u64, String> {
        Ok(self.behind)
    }
    fn checks_green(&self, _: u64) -> Result<(), String> {
        self.checks.clone()
    }
    fn land(&self, _: &GoRow) -> Result<String, String> {
        *self.lands.borrow_mut() += 1;
        self.land.clone()
    }
    fn post(&self, line: &str) -> Result<(), String> {
        self.posts.borrow_mut().push(line.into());
        Ok(())
    }
    fn close_merged_dry_run(&self, key: &str, _: &str) -> Result<String, String> {
        if key.ends_with("BAD") {
            Err("NO-WITNESS".into())
        } else {
            Ok("WOULD-CLOSE".into())
        }
    }
}

fn row() -> GoRow {
    parse_row(&format!("GO D-6401 #1090 {HEAD} plangraph:A plangraph:BAD")).expect("row")
}

#[test]
fn parses_a_go_row_and_rejects_ambiguous_lines() {
    let r = row();
    assert_eq!(
        (r.decision.as_str(), r.pr, r.head.as_str()),
        ("D-6401", 1090, HEAD)
    );
    assert_eq!(r.keys, ["plangraph:A", "plangraph:BAD"]);
    assert!(
        parse_row(&format!("D-1 1090 {HEAD}")).is_none(),
        "no GO token"
    );
    assert!(parse_row("GO D-1 1090 abc").is_none(), "short head");
    assert!(
        parse_row(&format!("GO D-1 D-2 1090 {HEAD}")).is_none(),
        "two decisions"
    );
    assert!(
        parse_row(&format!("GO D-1 {}", HEAD.to_uppercase())).is_none(),
        "upper-case head"
    );
}

#[test]
fn an_approver_row_lands_the_tested_green_head_without_a_router_decision() {
    let r = parse_row(&format!("GO by:attended-0042 #1090 {HEAD} plangraph:A")).expect("row");
    assert_eq!(r.decision, "by:attended-0042");
    let fake = Fake::green();
    let (outcome, lines) = handle(&r, &fake);
    assert_eq!(outcome, "MERGED");
    assert_eq!(*fake.lands.borrow(), 1);
    assert!(lines[0].ends_with(&format!("#1090 {HEAD} by:attended-0042")));
    assert!(
        lines[1].contains("merge-close closes the parts"),
        "{lines:?}"
    );
    assert_eq!(lines.len(), 2, "no router close-merged without a D-id");
    let moved = Fake {
        now: "f".repeat(40),
        ..Fake::green()
    };
    assert_eq!(
        handle(&r, &moved).0,
        "BLOCKED",
        "only the tested head lands"
    );
    assert!(
        parse_row(&format!("GO by: #1090 {HEAD}")).is_none(),
        "empty approver"
    );
    assert!(
        parse_row(&format!("GO by:op D-1 #1090 {HEAD}")).is_none(),
        "two approvals"
    );
}

#[test]
fn an_unmoved_green_head_lands_and_posts_merged_with_the_sha_and_the_dry_runs() {
    let fake = Fake::green();
    let (outcome, lines) = handle(&row(), &fake);
    assert_eq!(outcome, "MERGED");
    assert_eq!(*fake.lands.borrow(), 1);
    assert!(lines[0].starts_with(&format!("MERGED {} #1090 {HEAD} D-6401", "c".repeat(40))));
    assert!(lines[1].contains("plangraph:A D-6401: WOULD-CLOSE"));
    assert!(lines[2].contains("plangraph:BAD D-6401 refused: NO-WITNESS"));
    assert_eq!(*fake.posts.borrow(), lines);
}

#[test]
fn a_moved_head_a_red_check_or_a_stale_base_lands_nothing_and_names_the_reason() {
    let cases: Vec<(Fake, &str)> = vec![
        (
            Fake {
                now: "f".repeat(40),
                ..Fake::green()
            },
            "head moved",
        ),
        (
            Fake {
                behind: 3,
                ..Fake::green()
            },
            "3 commit(s) behind main",
        ),
        (
            Fake {
                checks: Err("ci-gate=fail".into()),
                ..Fake::green()
            },
            "ci-gate=fail",
        ),
        (
            Fake {
                land: Err("main_red".into()),
                ..Fake::green()
            },
            "land refused: main_red",
        ),
    ];
    for (fake, why) in cases {
        let (outcome, lines) = handle(&row(), &fake);
        assert_eq!(outcome, "BLOCKED", "{why}");
        assert!(
            lines[0].starts_with("BLOCKED land D-6401 #1090") && lines[0].contains(why),
            "{lines:?}"
        );
        assert!(!lines.iter().any(|l| l.starts_with("MERGED")));
        if why != "land refused: main_red" {
            assert_eq!(*fake.lands.borrow(), 0, "{why}: nothing landed");
        }
    }
}

#[test]
fn a_pass_handles_each_row_once_and_a_new_head_is_a_new_row() {
    let dir = tempfile::tempdir().expect("tmp");
    let (rows, done) = (dir.path().join("rows"), dir.path().join("done"));
    std::fs::write(&rows, format!("noise\nGO D-6401 #1090 {HEAD}\n")).expect("rows");
    let fake = Fake::green();
    let mut out = Vec::new();
    assert_eq!(pass(&rows, &done, &fake, &mut out), Ok(1));
    assert_eq!(pass(&rows, &done, &fake, &mut out), Ok(0), "same row again");
    assert_eq!(*fake.lands.borrow(), 1);
    let newer = "1".repeat(40);
    std::fs::write(
        &rows,
        format!("GO D-6401 #1090 {HEAD}\nGO D-6401 #1090 {newer}\n"),
    )
    .expect("rows");
    assert_eq!(pass(&rows, &done, &fake, &mut out), Ok(1));
    assert_eq!(
        pass(&dir.path().join("none"), &done, &fake, &mut out),
        Ok(0),
        "no row file"
    );
    assert!(std::fs::read_to_string(&done)
        .expect("done")
        .lines()
        .all(|l| l.starts_with("D-6401 ")));
}
