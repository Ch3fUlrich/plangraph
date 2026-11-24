use super::autofix::*;
use super::Task;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Command;

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .current_dir(dir)
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@invalid",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

/// A bare remote with branch `feature` holding rust/a.txt, and the head it has.
fn remote(root: &Path) -> PrHead {
    let bare = root.join("remote.git");
    let seed = root.join("seed");
    std::fs::create_dir_all(&seed).unwrap();
    git(
        root,
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "feature",
            bare.to_str().unwrap(),
        ],
    );
    git(&seed, &["init", "-q", "-b", "feature"]);
    std::fs::create_dir_all(seed.join("rust")).unwrap();
    std::fs::write(seed.join("rust/a.txt"), "ugly\n").unwrap();
    git(&seed, &["add", "-A"]);
    git(&seed, &["commit", "-q", "-m", "seed"]);
    git(&seed, &["remote", "add", "origin", bare.to_str().unwrap()]);
    git(&seed, &["push", "-q", "origin", "feature"]);
    PrHead {
        remote: bare.to_str().unwrap().into(),
        branch: "feature".into(),
        sha: git(&seed, &["rev-parse", "HEAD"]),
    }
}

fn request<'a>(head: &'a PrHead, command: &'a [&'a str]) -> Request<'a> {
    Request {
        head,
        command,
        dir: "rust",
        trailer: "Hand-Edit: ci-watch fmt fix of failed job 77",
        subject: "Apply the fmt fix of failed job 77",
    }
}

#[test]
fn a_fix_is_one_hand_edit_commit_naming_the_job_pushed_to_the_head_branch() {
    let root = tempfile::tempdir().unwrap();
    let head = remote(root.path());
    let scratch = root.path().join("scratch");
    let outcome = apply(
        &scratch,
        &request(&head, &["sh", "-c", "printf 'pretty\\n' > a.txt"]),
    )
    .unwrap();
    let Outcome::Pushed(sha) = outcome else {
        panic!("expected a push, got {outcome:?}")
    };
    let bare = root.path().join("remote.git");
    assert_eq!(git(&bare, &["rev-parse", "feature"]), sha);
    let message = git(&bare, &["log", "-1", "--format=%B", "feature"]);
    assert!(
        message.contains("\nHand-Edit: ci-watch fmt fix of failed job 77\n"),
        "{message}"
    );
    assert_eq!(
        git(&bare, &["rev-parse", "feature^"]),
        head.sha,
        "one commit on the red head"
    );
}

#[test]
fn a_fix_that_changes_nothing_is_too_large_or_touches_a_gate_file_pushes_nothing() {
    let root = tempfile::tempdir().unwrap();
    let head = remote(root.path());
    let bare = root.path().join("remote.git");
    let go = |name: &str, command: &[&str]| {
        apply(&root.path().join(name), &request(&head, command)).unwrap()
    };
    assert_eq!(go("a", &["true"]), Outcome::Unchanged);
    assert_eq!(
        go("b", &["sh", "-c", "seq 1 60 > a.txt"]),
        Outcome::TooLarge(61)
    );
    assert_eq!(
        go(
            "c",
            &[
                "sh",
                "-c",
                "mkdir -p ../.github/workflows && echo x > ../.github/workflows/t.yml"
            ]
        ),
        Outcome::GatePath(".github/workflows/t.yml".into())
    );
    let moved = PrHead {
        sha: "0".repeat(40),
        ..head.clone()
    };
    assert_eq!(
        apply(&root.path().join("d"), &request(&moved, &["true"])).unwrap(),
        Outcome::Stale
    );
    assert_eq!(
        git(&bare, &["rev-parse", "feature"]),
        head.sha,
        "nothing was pushed"
    );
}

fn task(head: &str, pr: u64, class: &str, job_id: u64, sha: &str) -> Task {
    Task {
        key: format!("k{job_id}"),
        head: head.into(),
        pr: Some(pr),
        class: class.into(),
        job_id,
        head_sha: sha.into(),
        ..Default::default()
    }
}

#[test]
fn one_automatic_fix_per_pull_request_and_class_then_the_writer() {
    let dir = tempfile::tempdir().unwrap();
    let tasks: BTreeMap<_, _> = [
        task("pr-9", 9, "fmt", 1, "abc"),
        task("pr-9", 9, "fmt", 2, "abc"),
        task("pr-9", 9, "code", 3, "abc"),
        task("pr-9", 9, "clippy", 4, "abc"),
    ]
    .into_iter()
    .map(|t| (t.key.clone(), t))
    .collect();
    let calls = std::cell::RefCell::new(Vec::new());
    let lookup = |_| {
        Ok(PrHead {
            remote: "r".into(),
            branch: "b".into(),
            sha: "abc".into(),
        })
    };
    let fix = |request: &Request, _: &Path| {
        calls.borrow_mut().push(request.trailer.to_owned());
        Ok(Outcome::Pushed("new".into()))
    };
    let rows = pass(dir.path(), &tasks, lookup, fix).unwrap();
    assert_eq!(rows.len(), 2, "fmt once and clippy once: {rows:?}");
    assert!(calls.borrow()[0].starts_with("Hand-Edit: ") && calls.borrow()[0].contains("job 1"));
    assert!(calls.borrow()[1].contains("clippy") && calls.borrow()[1].contains("job 4"));
    let again = pass(dir.path(), &tasks, lookup, fix).unwrap();
    assert!(again.is_empty(), "no loop: {again:?}");
    assert_eq!(calls.borrow().len(), 2);
}

#[test]
fn a_moved_head_makes_no_fix_and_keeps_the_class_open_for_the_next_red() {
    let dir = tempfile::tempdir().unwrap();
    let tasks: BTreeMap<_, _> = [task("pr-9", 9, "fmt", 1, "old")]
        .into_iter()
        .map(|t| (t.key.clone(), t))
        .collect();
    let lookup = |_| {
        Ok(PrHead {
            remote: "r".into(),
            branch: "b".into(),
            sha: "new".into(),
        })
    };
    let fix = |_: &Request, _: &Path| -> Result<Outcome, String> {
        panic!("a stale head is never fixed")
    };
    let rows = pass(dir.path(), &tasks, lookup, fix).unwrap();
    assert!(rows.is_empty(), "{rows:?}");
    let ledger = std::fs::read_to_string(dir.path().join("autofix.json")).unwrap_or_default();
    assert!(!ledger.contains("pr-9:fmt"), "nothing reserved: {ledger}");
}

struct Recorded(serde_json::Value, usize);
impl super::github::Github for Recorded {
    fn pages(&mut self, _: &str, _: Option<&str>) -> Result<Vec<serde_json::Value>, String> {
        unreachable!()
    }
    fn page(&mut self, _: &str, _: Option<&str>) -> Result<Vec<serde_json::Value>, String> {
        unreachable!()
    }
    fn log(&mut self, _: u64) -> Result<String, String> {
        unreachable!()
    }
    fn rerun(&mut self, _: u64) -> Result<(), String> {
        unreachable!()
    }
    fn value(&mut self, _: &str) -> Result<serde_json::Value, String> {
        self.1 += 1;
        Ok(self.0.clone())
    }
}

#[test]
fn a_closed_pull_request_is_never_cloned_and_is_read_once() {
    let pull = |state: &str| serde_json::json!({"state": state, "head": {"ref": "b", "sha": "abc", "repo": {"full_name": "o/r"}}});
    let mut open = Recorded(pull("open"), 0);
    assert_eq!(pr_head(&mut open, "o/r", 9).unwrap().sha, "abc");
    let mut closed = Recorded(pull("closed"), 0);
    assert_eq!(pr_head(&mut closed, "o/r", 9).err().unwrap(), NOT_OPEN);
    let dir = tempfile::tempdir().unwrap();
    let tasks: BTreeMap<_, _> = [
        task("pr-9", 9, "fmt", 1, "abc"),
        task("pr-9", 9, "clippy", 2, "abc"),
    ]
    .into_iter()
    .map(|t| (t.key.clone(), t))
    .collect();
    let fix = |_: &Request, _: &Path| -> Result<Outcome, String> {
        panic!("a closed pull request is never fixed")
    };
    let lookups = std::cell::Cell::new(0);
    let lookup = |pr| {
        lookups.set(lookups.get() + 1);
        pr_head(&mut closed, "o/r", pr)
    };
    assert!(pass(dir.path(), &tasks, lookup, fix).unwrap().is_empty());
    assert!(pass(dir.path(), &tasks, |_| panic!("read again"), fix)
        .unwrap()
        .is_empty());
    assert_eq!(lookups.get(), 1, "one read for both tasks");
}
