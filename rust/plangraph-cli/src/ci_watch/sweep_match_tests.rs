use super::github::Github;
use super::main_freeze_gh::{load_failures, route_owned};
use super::sweep_tests::{live, tasks, train, World};
use super::{scan_and_sweep, Result};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// Open pull requests with the exact runs GitHub lists for them (every run answers every query).
struct Listed {
    prs: Vec<Value>,
    runs: Vec<Value>,
}
impl Github for Listed {
    fn pages(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>> {
        World {
            prs: self.prs.clone(),
        }
        .pages(endpoint, field)
    }
    fn page(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>> {
        Ok(
            if endpoint.contains("branch=main") || endpoint.contains("head_sha=") {
                self.runs.clone()
            } else {
                vec![]
            },
        )
    }
    fn log(&mut self, _: u64) -> Result<String> {
        Ok("FAILED tests/a.py::test_a - AssertionError".into())
    }
    fn rerun(&mut self, _: u64) -> Result<()> {
        Ok(())
    }
}
fn run(id: u64, sha: &str, branch: &str, event: &str, pr: Option<u64>, conclusion: &str) -> Value {
    json!({"id": id, "head_sha": sha, "event": event, "head_branch": branch,
        "conclusion": conclusion,
        "pull_requests": pr.map(|n| vec![json!({"number": n})]).unwrap_or_default()})
}
fn watch(world: &mut Listed, dir: &Path) -> (Vec<Option<u64>>, Vec<String>) {
    let mut defined = Vec::new();
    let rows = scan_and_sweep(
        world,
        "o/r",
        dir,
        &BTreeMap::new(),
        false,
        |task| {
            defined.push(task.pr);
            Ok(true)
        },
        |task| route_owned(dir, task, 100),
        |_, _| Ok(()),
    )
    .unwrap();
    (defined, rows)
}
fn owned(dir: &Path, prs: Value) {
    std::fs::write(
        dir.join("owners.json"),
        json!({"prs": prs, "writers": {}}).to_string(),
    )
    .unwrap();
}

/// Runs belong to the pull request they name, even when several pull requests share the commit.
#[test]
fn a_train_failure_is_not_filed_against_an_unrelated_pull_request() {
    let temp = tempfile::tempdir().unwrap();
    owned(temp.path(), json!({"5": "batch-a"}));
    let mut world = Listed {
        prs: vec![train(5, "shared"), live(6, "shared")],
        runs: vec![run(
            10,
            "shared",
            "land/train-33",
            "pull_request",
            Some(5),
            "failure",
        )],
    };
    let (defined, rows) = watch(&mut world, temp.path());
    assert!(defined.is_empty(), "{rows:?}");
    let records = load_failures(temp.path()).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].pr, 5);
}

#[test]
fn an_ordinary_pull_requests_success_does_not_hide_a_train_failure_on_the_same_commit() {
    let temp = tempfile::tempdir().unwrap();
    owned(temp.path(), json!({"5": "batch-a"}));
    // The ordinary pull request's green run is the newest one on the commit.
    let mut world = Listed {
        prs: vec![train(5, "shared"), live(6, "shared")],
        runs: vec![
            run(
                12,
                "shared",
                "lane/thing",
                "pull_request",
                Some(6),
                "success",
            ),
            run(
                10,
                "shared",
                "land/train-33",
                "pull_request",
                Some(5),
                "failure",
            ),
        ],
    };
    let (defined, _) = watch(&mut world, temp.path());
    assert!(defined.is_empty());
    assert_eq!(load_failures(temp.path()).unwrap().len(), 1);
}

#[test]
fn a_push_to_main_is_never_routed_to_a_train_that_shares_its_commit() {
    let temp = tempfile::tempdir().unwrap();
    owned(temp.path(), json!({"5": "batch-a"}));
    let mut world = Listed {
        prs: vec![train(5, "tip")],
        runs: vec![run(10, "tip", "main", "push", None, "failure")],
    };
    let (defined, rows) = watch(&mut world, temp.path());
    assert_eq!(defined, [None], "main files its own item: {rows:?}");
    assert!(
        load_failures(temp.path()).unwrap().is_empty(),
        "the train owner never hears of main"
    );
    assert_eq!(tasks(temp.path())[0].head, "main");
}

/// Without a pull request list on the run, the branch and repository it ran in pick the pull request.
#[test]
fn a_run_without_a_pull_request_list_is_matched_by_its_branch_and_repository() {
    let temp = tempfile::tempdir().unwrap();
    owned(temp.path(), json!({"5": "batch-a"}));
    let mut unnamed = run(10, "shared", "lane/thing", "pull_request", None, "failure");
    unnamed["head_repository"] = json!({"full_name": "o/r"});
    let mut world = Listed {
        prs: vec![
            fork_pr(5, "shared", "land/train-33", "o/r"),
            fork_pr(6, "shared", "lane/thing", "o/r"),
        ],
        runs: vec![unnamed],
    };
    let (defined, _) = watch(&mut world, temp.path());
    assert_eq!(defined, [Some(6)]);
    assert!(load_failures(temp.path()).unwrap().is_empty());
}

fn fork_pr(number: u64, sha: &str, branch: &str, repo: &str) -> Value {
    json!({"number": number, "title": "Fix", "draft": false,
        "head": {"sha": sha, "ref": branch, "repo": {"full_name": repo}}})
}

/// Two forks can share a branch name and a commit: a run that names no pull request belongs to the one
/// whose head repository it ran in, and to none when that cannot be told.
#[test]
fn a_run_without_pull_request_metadata_needs_the_branch_and_the_head_repository() {
    let both = vec![
        fork_pr(5, "same", "fix", "alice/r"),
        fork_pr(6, "same", "fix", "bob/r"),
    ];
    let mut run = run(10, "same", "fix", "pull_request", None, "failure");
    // Ran in bob's fork: only #6.
    run["head_repository"] = json!({"full_name": "bob/r"});
    let temp = tempfile::tempdir().unwrap();
    let mut world = Listed {
        prs: both.clone(),
        runs: vec![run.clone()],
    };
    let (defined, _) = watch(&mut world, temp.path());
    assert_eq!(defined, [Some(6)]);
    // No head repository on the run and two pull requests share branch and commit: unassigned.
    run.as_object_mut().unwrap().remove("head_repository");
    let temp = tempfile::tempdir().unwrap();
    let mut world = Listed {
        prs: both,
        runs: vec![run],
    };
    let (defined, rows) = watch(&mut world, temp.path());
    assert!(defined.is_empty(), "{rows:?}");
    assert!(tasks(temp.path()).is_empty());
}

#[test]
fn the_only_pull_request_on_a_commit_does_not_take_a_run_of_another_branch() {
    let temp = tempfile::tempdir().unwrap();
    let mut world = Listed {
        prs: vec![live(6, "shared")],
        runs: vec![run(
            10,
            "shared",
            "other-branch",
            "pull_request",
            None,
            "failure",
        )],
    };
    let (defined, rows) = watch(&mut world, temp.path());
    assert!(defined.is_empty(), "{rows:?}");
}

/// A single candidate is not enough: both the run and the pull request must say where the branch lives.
#[test]
fn a_single_candidate_is_assigned_only_when_both_repositories_are_known_and_equal() {
    let outcome = |pr_repo: Option<&str>, run_repo: Option<&str>| {
        let mut candidate = fork_pr(6, "tip", "fix", "o/r");
        match pr_repo {
            Some(repo) => candidate["head"]["repo"] = json!({"full_name": repo}),
            None => {
                candidate["head"].as_object_mut().unwrap().remove("repo");
            }
        }
        let mut unnamed = run(10, "tip", "fix", "pull_request", None, "failure");
        if let Some(repo) = run_repo {
            unnamed["head_repository"] = json!({"full_name": repo});
        }
        let temp = tempfile::tempdir().unwrap();
        let mut world = Listed {
            prs: vec![candidate],
            runs: vec![unnamed],
        };
        watch(&mut world, temp.path()).0
    };
    assert_eq!(outcome(Some("o/r"), Some("o/r")), [Some(6)]);
    assert!(
        outcome(None, Some("o/r")).is_empty(),
        "the pull request names no repository"
    );
    assert!(
        outcome(Some("o/r"), None).is_empty(),
        "the run names no repository"
    );
    assert!(
        outcome(Some("o/r"), Some("fork/r")).is_empty(),
        "another repository"
    );
}
