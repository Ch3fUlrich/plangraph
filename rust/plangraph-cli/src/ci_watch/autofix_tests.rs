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
        current: true,
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
    let rows = pass(dir.path(), "o/r", &tasks, lookup, |_| Ok(true), fix).unwrap();
    assert_eq!(rows.len(), 2, "fmt once and clippy once: {rows:?}");
    assert!(calls.borrow()[0].starts_with("Hand-Edit: ") && calls.borrow()[0].contains("job 1"));
    assert!(calls.borrow()[1].contains("clippy") && calls.borrow()[1].contains("job 4"));
    let again = pass(dir.path(), "o/r", &tasks, lookup, |_| Ok(true), fix).unwrap();
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
    let rows = pass(dir.path(), "o/r", &tasks, lookup, |_| Ok(true), fix).unwrap();
    assert!(rows.is_empty(), "{rows:?}");
    let ledger = std::fs::read_to_string(dir.path().join("autofix.json")).unwrap_or_default();
    for sha in ["old", "new"] {
        assert!(
            !ledger.contains(&format!("o/r@{sha}")),
            "stale head {sha} reserved: {ledger}"
        );
    }
}

#[test]
fn a_run_that_is_no_longer_red_is_never_fixed_nor_reserved() {
    let dir = tempfile::tempdir().unwrap();
    let tasks: BTreeMap<_, _> = [task("pr-9", 9, "fmt", 1, "abc")]
        .into_iter()
        .map(|t| (t.key.clone(), t))
        .collect();
    let lookup = |_| {
        Ok(PrHead {
            remote: "r".into(),
            branch: "b".into(),
            sha: "abc".into(),
        })
    };
    // Another scan recorded the rerun green after these tasks were read.
    let rows = pass(
        dir.path(),
        "o/r",
        &tasks,
        lookup,
        |run| {
            assert_eq!(run, 0, "the run id of the task");
            Ok(false)
        },
        |_: &Request, _: &Path| -> Result<Outcome, String> { panic!("a green run is never fixed") },
    )
    .unwrap();
    assert!(rows.is_empty());
    let ledger = std::fs::read_to_string(dir.path().join("autofix.json")).unwrap_or_default();
    assert!(!ledger.contains("o/r@abc"), "{ledger}");
}

struct Run(&'static str);
impl super::github::Github for Run {
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
    fn value(&mut self, endpoint: &str) -> Result<serde_json::Value, String> {
        assert_eq!(endpoint, "actions/runs/7");
        Ok(
            serde_json::json!({"conclusion": serde_json::Value::String(self.0.into()), "status": "completed"}),
        )
    }
}

#[test]
fn only_a_run_whose_latest_attempt_concluded_red_is_red() {
    assert!(run_is_red(&mut Run("failure"), 7).unwrap());
    assert!(run_is_red(&mut Run("timed_out"), 7).unwrap());
    assert!(!run_is_red(&mut Run("success"), 7).unwrap());
    assert!(
        !run_is_red(&mut Run(""), 7).unwrap(),
        "a rerun in flight has no conclusion"
    );
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
    assert!(pass(dir.path(), "o/r", &tasks, lookup, |_| Ok(true), fix)
        .unwrap()
        .is_empty());
    assert!(pass(
        dir.path(),
        "o/r",
        &tasks,
        |_| panic!("read again"),
        |_| Ok(true),
        fix
    )
    .unwrap()
    .is_empty());
    assert_eq!(lookups.get(), 1, "one read for both tasks");
}

#[test]
fn the_protected_paths_equal_the_commit_gates_hand_edit_and_gate_lists() {
    let source = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py"),
    )
    .unwrap();
    let mut python = Vec::new();
    for name in ["AUTH_DOOR_PATHS", "GATE_PATHS"] {
        let start = source
            .find(&format!("{name}: tuple[str, ...] = ("))
            .unwrap();
        let body = &source[start..start + source[start..].find("\n)").unwrap()];
        for line in body.lines().skip(1) {
            let line = line.split('#').next().unwrap().trim();
            if let Some(item) = line.strip_prefix('\'').and_then(|l| l.split('\'').next()) {
                python.push(item.to_owned());
            }
        }
    }
    python.sort();
    python.dedup();
    let mut rust: Vec<String> = PROTECTED.iter().map(|p| (*p).to_owned()).collect();
    rust.sort();
    assert_eq!(
        rust, python,
        "update PROTECTED with tools/ci_commit_gate.py"
    );
}

#[test]
fn a_fix_touching_the_go_verifier_or_a_gitattributes_file_is_dropped() {
    let root = tempfile::tempdir().unwrap();
    let head = remote(root.path());
    let go = |name: &str, command: &str| {
        apply(
            &root.path().join(name),
            &request(&head, &["sh", "-c", command]),
        )
        .unwrap()
    };
    assert_eq!(
        go(
            "a",
            "mkdir -p plangraph-cli/src/land && echo x > plangraph-cli/src/land/go_verify.rs"
        ),
        Outcome::GatePath("rust/plangraph-cli/src/land/go_verify.rs".into())
    );
    assert_eq!(
        go("b", "mkdir -p sub && echo x > sub/.gitattributes"),
        Outcome::GatePath("rust/sub/.gitattributes".into())
    );
    assert!(
        matches!(go("c", "echo y >> a.txt"), Outcome::Pushed(_)),
        "an ordinary file is still fixed"
    );
}

#[test]
fn the_fix_command_runs_without_the_watchers_home_and_cannot_hook_the_commit() {
    let root = tempfile::tempdir().unwrap();
    let head = remote(root.path());
    let marker = root.path().join("hook-ran");
    // The command writes the HOME it saw, plants a pre-commit and pre-push hook and a credential helper in its
    // own checkout; none of them may run, and the pushed file must name the scratch HOME.
    let script = format!(
        "printf '%s\\n' \"$HOME\" > a.txt; \
         mkdir -p ../.git/hooks; \
         printf '#!/bin/sh\\ntouch {m}\\n' > ../.git/hooks/pre-commit; chmod +x ../.git/hooks/pre-commit; \
         cp ../.git/hooks/pre-commit ../.git/hooks/pre-push; \
         git -C .. config core.hooksPath hooks; git -C .. config core.fsmonitor 'touch {m}'",
        m = marker.display()
    );
    let scratch = root.path().join("scratch");
    let outcome = apply(&scratch, &request(&head, &["sh", "-c", &script])).unwrap();
    assert!(matches!(outcome, Outcome::Pushed(_)), "{outcome:?}");
    assert!(!marker.exists(), "a hook or fsmonitor of the sandbox ran");
    let bare = root.path().join("remote.git");
    let pushed = git(&bare, &["show", "feature:rust/a.txt"]);
    let real_home = std::env::var("HOME").unwrap_or_default();
    assert!(pushed.starts_with(scratch.to_str().unwrap()), "{pushed}");
    assert!(real_home.is_empty() || pushed != real_home);
    let tracked = git(&bare, &["ls-tree", "-r", "--name-only", "feature"]);
    assert!(!tracked.contains(".git/"), "{tracked}");
}

#[test]
fn a_head_gets_one_fix_per_class_a_later_head_gets_its_own_and_the_fixed_head_none() {
    let dir = tempfile::tempdir().unwrap();
    let sha = std::cell::RefCell::new("s1".to_owned());
    let lookup = |_| {
        Ok(PrHead {
            remote: "r".into(),
            branch: "b".into(),
            sha: sha.borrow().clone(),
        })
    };
    let pushed = std::cell::RefCell::new(Vec::new());
    let fix = |_: &Request, _: &Path| {
        let new = format!("fix{}", pushed.borrow().len());
        pushed.borrow_mut().push(new.clone());
        Ok(Outcome::Pushed(new))
    };
    let at = |sha: &str, job: u64| -> BTreeMap<String, Task> {
        [task("pr-9", 9, "fmt", job, sha)]
            .into_iter()
            .map(|t| (t.key.clone(), t))
            .collect()
    };
    pass(dir.path(), "o/r", &at("s1", 1), lookup, |_| Ok(true), fix).unwrap();
    assert_eq!(pushed.borrow().len(), 1);
    // The head the fix produced is still red with the same class: never fixed again.
    *sha.borrow_mut() = "fix0".into();
    assert!(
        pass(dir.path(), "o/r", &at("fix0", 2), lookup, |_| Ok(true), fix)
            .unwrap()
            .is_empty()
    );
    // A person's newer head that is red the same way gets its own fix.
    *sha.borrow_mut() = "s2".into();
    pass(dir.path(), "o/r", &at("s2", 3), lookup, |_| Ok(true), fix).unwrap();
    assert_eq!(pushed.borrow().len(), 2);
}

#[test]
fn a_task_that_is_not_current_is_never_fixed() {
    let dir = tempfile::tempdir().unwrap();
    let mut old = task("pr-9", 9, "fmt", 1, "abc");
    old.current = false;
    let tasks: BTreeMap<_, _> = [(old.key.clone(), old)].into_iter().collect();
    let rows = pass(
        dir.path(),
        "o/r",
        &tasks,
        |_| panic!("a green head is not read"),
        |_| panic!("a green head is not asked"),
        |_: &Request, _: &Path| -> Result<Outcome, String> { panic!("a green head is not fixed") },
    )
    .unwrap();
    assert!(rows.is_empty());
}

#[test]
fn the_scheduled_service_runs_the_automatic_fix() {
    let unit = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/systemd/pg-ci-watch.service"),
    )
    .unwrap();
    let exec = unit.lines().find(|l| l.starts_with("ExecStart=")).unwrap();
    for flag in ["ci-watch", "--keeper", "--main-freeze", "--autofix"] {
        assert!(exec.contains(flag), "{flag} missing from {exec}");
    }
}

#[cfg(target_os = "linux")]
#[test]
fn the_fix_command_cannot_read_the_trusted_clone_or_a_file_of_the_watcher_nor_write_outside_its_checkout(
) {
    let root = tempfile::tempdir().unwrap();
    let head = remote(root.path());
    let secret = root.path().join("watcher-secret.txt");
    std::fs::write(&secret, "token-value\n").unwrap();
    let scratch = root.path().join("scratch");
    let escape = root.path().join("escaped.txt");
    // The trusted clone is `scratch/trusted`; the command knows where it is and still cannot read or write it.
    let script = format!(
        "{{ cat '{s}' 2>&1; cat '{t}/rust/a.txt' 2>&1; echo x > '{e}' 2>&1; echo x > '{t}/rust/b.txt' 2>&1; echo end; }} > a.txt",
        s = secret.display(),
        t = scratch.join("trusted").display(),
        e = escape.display()
    );
    let outcome = apply(&scratch, &request(&head, &["sh", "-c", &script])).unwrap();
    assert!(matches!(outcome, Outcome::Pushed(_)), "{outcome:?}");
    let bare = root.path().join("remote.git");
    let seen = git(&bare, &["show", "feature:rust/a.txt"]);
    assert!(seen.contains("end"), "{seen}");
    assert!(
        !seen.contains("token-value"),
        "the watcher's file was readable: {seen}"
    );
    assert!(
        !seen.contains("ugly"),
        "the trusted clone was readable: {seen}"
    );
    assert!(!escape.exists(), "the command wrote outside its checkout");
    assert!(!scratch.join("trusted/rust/b.txt").exists());
}
