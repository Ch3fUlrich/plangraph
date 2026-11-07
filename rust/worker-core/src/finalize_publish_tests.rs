use super::*;
use std::cell::Cell;
struct FakePr(Cell<usize>);
impl PrStep for FakePr {
    fn find_or_update(&self, _: &str, _: &str, _: &str, _: &str) -> Result<PrRef> {
        self.0.set(self.0.get() + 1);
        Ok(PrRef { number: 42 })
    }
}
struct Fixture {
    dir: tempfile::TempDir,
    head: String,
    old: String,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let remote = Remote {
            root: dir.path(),
            name: "./remote.git",
        };
        assert!(git(&remote, &["init", "--bare", "remote.git"])
            .unwrap()
            .status
            .success());
        assert!(git(&remote, &["init", "."]).unwrap().status.success());
        let commit = |parent: Option<&str>| {
            let tree = crate::writer_secret::command(dir.path(), &["mktree"], b"", None).unwrap();
            let tree = std::str::from_utf8(&tree).unwrap().trim();
            let mut args = vec!["commit-tree", tree];
            if let Some(parent) = parent {
                args.extend(["-p", parent]);
            }
            String::from_utf8(
                crate::writer_secret::command(dir.path(), &args, b"test\n", None).unwrap(),
            )
            .unwrap()
            .trim()
            .to_owned()
        };
        let old = commit(None);
        let head = commit(Some(&old));
        Self { dir, head, old }
    }
    fn remote(&self) -> Remote<'_> {
        Remote {
            root: self.dir.path(),
            name: "./remote.git",
        }
    }
    fn log(&self) -> LocalFileStepLog {
        LocalFileStepLog {
            dir: self.dir.path().join("logs"),
        }
    }
    fn run(&self, pr: &FakePr) -> Outcome {
        finalize_publish(
            "run",
            &self.head,
            "lane",
            None,
            &self.remote(),
            pr,
            &self.log(),
        )
        .unwrap()
    }
}
#[test]
fn expected_absent_and_already_published() {
    let f = Fixture::new();
    assert_eq!(
        publish(&f.head, "lane", None, &f.remote()).unwrap(),
        Outcome::Published
    );
    assert_eq!(
        publish(&f.head, "lane", None, &f.remote()).unwrap(),
        Outcome::AlreadyPublished
    );
}
#[test]
fn expected_old_match() {
    let f = Fixture::new();
    publish(&f.old, "lane", None, &f.remote()).unwrap();
    assert_eq!(
        publish(&f.head, "lane", Some(&f.old), &f.remote()).unwrap(),
        Outcome::Published
    );
}
#[test]
fn moved_lane_conflict_is_terminal() {
    let f = Fixture::new();
    publish(&f.old, "lane", None, &f.remote()).unwrap();
    let pr = FakePr(Cell::new(0));
    assert_eq!(f.run(&pr), Outcome::Conflict);
    assert_eq!(f.run(&pr), Outcome::Conflict);
    assert_eq!(pr.0.get(), 0);
    assert_eq!(
        lane_head(&f.remote(), "refs/heads/lane").unwrap(),
        Some(f.old.clone())
    );
    assert_eq!(f.log().load("run").unwrap().len(), 1);
}
#[test]
fn recovery_after_cas_and_done_skips_calls() {
    let f = Fixture::new();
    publish(&f.head, "lane", None, &f.remote()).unwrap();
    f.log()
        .append(
            "run",
            Step::LaneCas {
                head: f.head.clone(),
                expected_old: None,
                outcome: Outcome::Published,
            },
        )
        .unwrap();
    // Remove the remote entirely: recovery must not perform even a Git read.
    fs::rename(
        f.dir.path().join("remote.git"),
        f.dir.path().join("offline.git"),
    )
    .unwrap();
    let pr = FakePr(Cell::new(0));
    assert_eq!(f.run(&pr), Outcome::Published);
    assert_eq!(pr.0.get(), 1);
    assert_eq!(f.run(&pr), Outcome::Published);
    assert_eq!(pr.0.get(), 1);
    assert_eq!(f.log().load("run").unwrap().len(), 3);
}
#[test]
fn torn_line_is_ignored_and_repaired() {
    let f = Fixture::new();
    let log = f.log();
    log.append("run", Step::Pr { number: 1 }).unwrap();
    OpenOptions::new()
        .append(true)
        .open(log.path("run").unwrap())
        .unwrap()
        .write_all(b"{torn")
        .unwrap();
    assert_eq!(log.load("run").unwrap(), vec![Step::Pr { number: 1 }]);
    log.append("run", Step::Done).unwrap();
    assert_eq!(
        log.load("run").unwrap(),
        vec![Step::Pr { number: 1 }, Step::Done]
    );
}
#[test]
fn bad_run_id_refused() {
    let f = Fixture::new();
    for id in ["", "../escape", "a/b", ".", "a\n"] {
        assert!(f.log().load(id).is_err());
        assert!(f.log().append(id, Step::Done).is_err());
    }
}
