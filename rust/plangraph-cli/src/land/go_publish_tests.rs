use super::build_tests::g;
use super::go_publish::*;
use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
use std::process::Command;

const HEAD: &str = "1111111111111111111111111111111111111111";
const HEADER: &str = "| ID | project | decision | by | why |\n|---|---|---|---|---|\n";

fn go() -> GoRow {
    GoRow {
        project: "demo".into(),
        pr: 1234,
        head: HEAD.into(),
        by: "router".into(),
        why: "computed".into(),
    }
}

/// A bare "origin" with the decisions file on `main` and a working clone that fetches from it.
struct World {
    _dir: tempfile::TempDir,
    origin: PathBuf,
    work: PathBuf,
    other: PathBuf,
}

fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let origin = dir.path().join("origin.git");
    let seed = dir.path().join("seed");
    std::fs::create_dir_all(&seed).unwrap();
    g(
        dir.path(),
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            origin.to_str().unwrap(),
        ],
    );
    g(&seed, &["init", "-q", "-b", "main"]);
    std::fs::write(
        seed.join("DECISIONS.md"),
        format!("{HEADER}| D-1 | demo | first | op | |\n| D-2 | demo | second | op | |\n"),
    )
    .unwrap();
    g(&seed, &["add", "-A"]);
    g(&seed, &["commit", "-q", "-m", "seed"]);
    g(&seed, &["push", "-q", origin.to_str().unwrap(), "main"]);
    let work = dir.path().join("work");
    let other = dir.path().join("other");
    for p in [&work, &other] {
        g(
            dir.path(),
            &["clone", "-q", origin.to_str().unwrap(), p.to_str().unwrap()],
        );
    }
    World {
        _dir: dir,
        origin,
        work,
        other,
    }
}

/// Another decision lands on `main` through a second clone.
fn competing(w: &World, id: &str) {
    let file = w.other.join("DECISIONS.md");
    let text = std::fs::read_to_string(&file).unwrap();
    std::fs::write(
        &file,
        format!("{text}| {id} | other | someone else | op | |\n"),
    )
    .unwrap();
    g(&w.other, &["commit", "-qam", id]);
    g(&w.other, &["push", "-q", "origin", "main"]);
}

/// The trusted principal: fast-forwards `main` to the row branch only while `main` is at the tip.
struct Principal<'a> {
    world: &'a World,
    race_once: Cell<Option<&'static str>>,
    merges: Cell<usize>,
}

impl RowPr for Principal<'_> {
    fn merge(&self, pr_ref: &str, tip: &str) -> Result<(), Submit> {
        if let Some(id) = self.race_once.take() {
            competing(self.world, id);
        }
        let now = g(&self.world.origin, &["rev-parse", "refs/heads/main"]);
        if now != tip {
            return Err(Submit::Moved);
        }
        let row = g(&self.world.origin, &["rev-parse", pr_ref]);
        g(
            &self.world.origin,
            &["update-ref", "refs/heads/main", &row, tip],
        );
        self.merges.set(self.merges.get() + 1);
        Ok(())
    }
}

fn principal<'a>(w: &'a World, race: Option<&'static str>) -> Principal<'a> {
    Principal {
        world: w,
        race_once: Cell::new(race),
        merges: Cell::new(0),
    }
}

fn keeper_python() -> Command {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let mut c = Command::new(std::env::var("PG_TEST_PYTHON").unwrap_or_else(|_| "python3".into()));
    c.env("PYTHONPATH", root);
    c
}

fn main_text(w: &World) -> String {
    g(&w.origin, &["show", "refs/heads/main:DECISIONS.md"]) + "\n"
}

fn fresh(_: &str) -> Result<(), String> {
    Ok(())
}

#[test]
fn a_fixture_publishes_one_row_the_canonical_reader_accepts() {
    let w = world();
    let p = principal(&w, None);
    let branch = GitDecisions {
        repo: &w.work,
        remote: "origin",
        branch: "main",
        path: "DECISIONS.md",
        principal: &p,
    };
    let reader = KeeperReader {
        python: &keeper_python,
    };
    let done = publish(&branch, &reader, &go(), &fresh, 3).unwrap();
    assert_eq!(done.d_id, "D-3");
    assert_eq!(done.attempts, 1);
    assert_eq!(
        main_text(&w),
        format!(
            "{HEADER}| D-1 | demo | first | op | |\n| D-2 | demo | second | op | |\n{}\n",
            go().line("D-3")
        )
    );
    assert_eq!(done.tip, g(&w.origin, &["rev-parse", "refs/heads/main"]));
    // Only the principal moved the protected branch; the publisher left a row branch behind.
    assert_eq!(p.merges.get(), 1);
    assert!(!g(&w.origin, &["for-each-ref", "refs/heads/go-row/"]).is_empty());
}

#[test]
fn a_colliding_d_id_is_taken_by_the_next_free_id_and_the_other_decision_stays() {
    let w = world();
    let p = principal(&w, Some("D-3"));
    let branch = GitDecisions {
        repo: &w.work,
        remote: "origin",
        branch: "main",
        path: "DECISIONS.md",
        principal: &p,
    };
    let reader = KeeperReader {
        python: &keeper_python,
    };
    let done = publish(&branch, &reader, &go(), &fresh, 3).unwrap();
    assert_eq!((done.d_id.as_str(), done.attempts), ("D-4", 2));
    let text = main_text(&w);
    assert!(
        text.contains("| D-3 | other | someone else | op | |\n"),
        "{text}"
    );
    assert_eq!(text.matches(&go().line("D-4")).count(), 1);
    assert_eq!(text.matches("| D-3 |").count(), 1);
}

#[test]
fn a_go_that_went_stale_while_the_branch_moved_is_not_published_on_the_retry() {
    let w = world();
    let p = principal(&w, Some("D-3"));
    let branch = GitDecisions {
        repo: &w.work,
        remote: "origin",
        branch: "main",
        path: "DECISIONS.md",
        principal: &p,
    };
    let reader = KeeperReader {
        python: &keeper_python,
    };
    let calls = Cell::new(0);
    let stale_second = |_: &str| {
        calls.set(calls.get() + 1);
        if calls.get() == 1 {
            Ok(())
        } else {
            Err("main moved past the GO's base".to_string())
        }
    };
    let err = publish(&branch, &reader, &go(), &stale_second, 3).unwrap_err();
    assert!(err.starts_with("stale: main moved"), "{err}");
    assert_eq!(p.merges.get(), 0);
    assert!(!main_text(&w).contains("GO-HEAD"));
}

#[test]
fn a_missing_protected_branch_is_refused() {
    let w = world();
    let p = principal(&w, None);
    let branch = GitDecisions {
        repo: &w.work,
        remote: "origin",
        branch: "no-such",
        path: "DECISIONS.md",
        principal: &p,
    };
    let err = publish(
        &branch,
        &KeeperReader {
            python: &keeper_python,
        },
        &go(),
        &fresh,
        3,
    )
    .unwrap_err();
    assert!(err.contains("cannot be read"), "{err}");
}

/// A branch whose read after the submission is scripted: the principal said yes, the branch shows what it shows.
struct Scripted {
    reads: RefCell<Vec<String>>,
    submitted: RefCell<Vec<String>>,
}

impl Decisions for Scripted {
    fn read(&self) -> Result<(String, String), String> {
        let mut reads = self.reads.borrow_mut();
        let text = if reads.len() > 1 {
            reads.remove(0)
        } else {
            reads[0].clone()
        };
        Ok(("tip".into(), text))
    }
    fn submit(&self, _: &str, _: &str, text: &str) -> Result<(), Submit> {
        self.submitted.borrow_mut().push(text.to_owned());
        Ok(())
    }
}

struct Binds(Option<(u64, String)>);
impl Reader for Binds {
    fn bound(&self, _: &str, _: &str, _: &str) -> Result<Option<(u64, String)>, String> {
        Ok(self.0.clone())
    }
}

fn scripted(after: &str) -> Scripted {
    let before = format!("{HEADER}| D-1 | demo | first | op | |\n");
    Scripted {
        reads: RefCell::new(vec![before, after.to_owned()]),
        submitted: RefCell::new(vec![]),
    }
}

#[test]
fn a_read_back_without_the_row_or_with_another_row_or_a_changed_line_is_refused() {
    let before = format!("{HEADER}| D-1 | demo | first | op | |\n");
    let row = go().line("D-2");
    let bound = Binds(Some((1234, HEAD.into())));
    let missing = publish(&scripted(&before), &bound, &go(), &fresh, 1).unwrap_err();
    assert!(
        missing.contains("D-2 is not on the protected branch"),
        "{missing}"
    );
    let other = format!("{before}| D-2 | demo | something else | op | |\n");
    let mismatch = publish(&scripted(&other), &bound, &go(), &fresh, 1).unwrap_err();
    assert!(mismatch.contains("is another decision"), "{mismatch}");
    let changed = format!("{HEADER}| D-1 | demo | rewritten | op | |\n{row}\n");
    let rewritten = publish(&scripted(&changed), &bound, &go(), &fresh, 1).unwrap_err();
    assert!(
        rewritten.contains("an earlier decision line changed"),
        "{rewritten}"
    );
    let twice = format!("{before}{row}\n{row}\n");
    assert!(publish(&scripted(&twice), &bound, &go(), &fresh, 1)
        .unwrap_err()
        .contains("more than once"));
    let good = format!("{before}{row}\n");
    assert!(publish(&scripted(&good), &bound, &go(), &fresh, 1).is_ok());
    // The canonical reader must bind the row to this PR and head.
    let unbound = publish(&scripted(&good), &Binds(None), &go(), &fresh, 1).unwrap_err();
    assert!(unbound.contains("not GO #1234"), "{unbound}");
    let other_head = Binds(Some((1234, "2".repeat(40))));
    assert!(publish(&scripted(&good), &other_head, &go(), &fresh, 1).is_err());
}

#[test]
fn ids_are_allocated_above_the_highest_row_outside_fences() {
    assert_eq!(next_id(""), "D-1");
    assert_eq!(next_id("| D-7 | a |\n| D-3 | b |\n"), "D-8");
    assert_eq!(
        next_id("| D-2 | a |\n```\n| D-90 | example |\n```\n"),
        "D-3"
    );
}
