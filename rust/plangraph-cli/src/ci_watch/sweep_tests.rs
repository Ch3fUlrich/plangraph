use super::github::Github;
use super::main_freeze_gh::{load_failures, route_owned};
use super::sweep::{not_a_fix_lane_pr, sweep};
use super::{cancel_item, load_tasks, save_tasks, scan, scan_and_sweep, Result, Task};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// Open pull requests plus one failed run on each head; a PR missing from `prs` is closed.
pub(super) struct World {
    pub(super) prs: Vec<Value>,
}
impl Github for World {
    fn pages(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>> {
        if endpoint.starts_with("pulls?") {
            Ok(self.prs.clone())
        } else {
            Ok(vec![
                json!({"id": 100, "name": "tests", "conclusion": "failure"}),
            ])
        }
    }
    fn page(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>> {
        let mut runs = Vec::new();
        for pr in &self.prs {
            let sha = pr["head"]["sha"].as_str().unwrap();
            if endpoint.ends_with(&format!("head_sha={sha}")) {
                runs.push(
                    json!({"id": run_id(sha), "head_sha": sha, "event": "pull_request",
                    "head_branch": pr["head"]["ref"], "conclusion": "failure",
                    "pull_requests": [{"number": pr["number"]}]}),
                );
            }
        }
        Ok(runs)
    }
    fn log(&mut self, _: u64) -> Result<String> {
        Ok("FAILED tests/a.py::test_a - AssertionError".into())
    }
    fn rerun(&mut self, _: u64) -> Result<()> {
        Ok(())
    }
}

/// Each head has its own failing run.
fn run_id(sha: &str) -> u64 {
    100 + sha.len() as u64
}

pub(super) fn pr(number: u64, sha: &str, title: &str, draft: bool, branch: &str) -> Value {
    json!({"number": number, "title": title, "draft": draft,
        "head": {"sha": sha, "ref": branch}})
}
pub(super) fn live(number: u64, sha: &str) -> Value {
    pr(number, sha, "Fix the thing", false, "lane/thing")
}
pub(super) fn tasks(dir: &Path) -> Vec<Task> {
    load_tasks(&dir.join("tasks.jsonl"))
        .unwrap()
        .into_values()
        .collect()
}
fn scan_all(world: &mut World, dir: &Path) -> Vec<String> {
    scan(world, "o/r", dir, &BTreeMap::new(), false, |_| Ok(true)).unwrap()
}

#[test]
fn failed_runs_of_draft_probe_and_train_prs_file_no_item() {
    for excluded in [
        pr(5, "s5", "Fix", true, "lane/x"),
        pr(5, "s5", "CI probe: part 3", false, "probe/x"),
        pr(5, "s5", "Landing train 33", false, "land/train-33"),
    ] {
        assert!(not_a_fix_lane_pr(&excluded), "{excluded}");
        let temp = tempfile::tempdir().unwrap();
        scan_all(
            &mut World {
                prs: vec![excluded],
            },
            temp.path(),
        );
        assert!(tasks(temp.path()).is_empty());
    }
    let temp = tempfile::tempdir().unwrap();
    scan_all(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
    );
    assert_eq!(tasks(temp.path())[0].sha.as_deref(), Some("s5"));
}

#[test]
fn closing_the_pull_request_cancels_its_open_items() {
    let temp = tempfile::tempdir().unwrap();
    scan_all(
        &mut World {
            prs: vec![live(5, "s5"), live(6, "s6")],
        },
        temp.path(),
    );
    assert_eq!(tasks(temp.path()).len(), 2);
    let mut cancelled = Vec::new();
    let rows = sweep(
        &mut World {
            prs: vec![live(6, "s6")],
        },
        temp.path(),
        false,
        |key, reason| {
            cancelled.push((key.to_owned(), reason.to_owned()));
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(cancelled.len(), 1);
    assert!(cancelled[0].1.contains("#5"), "{cancelled:?}");
    let left = tasks(temp.path());
    assert_eq!(left.len(), 1);
    assert_eq!(left[0].pr, Some(6));
}

#[test]
fn a_new_head_or_a_probe_conversion_cancels_the_old_items() {
    let temp = tempfile::tempdir().unwrap();
    scan_all(
        &mut World {
            prs: vec![live(5, "s5"), live(6, "s6")],
        },
        temp.path(),
    );
    let mut cancelled = 0;
    let world = World {
        prs: vec![
            live(5, "s5-new"),
            pr(6, "s6", "CI probe: x", false, "probe/x"),
        ],
    };
    let rows = sweep(&mut { world }, temp.path(), false, |_, _| {
        cancelled += 1;
        Ok(())
    })
    .unwrap();
    assert_eq!((rows.len(), cancelled), (2, 2));
    assert!(tasks(temp.path()).is_empty());
}

#[test]
fn a_main_item_a_current_head_and_a_failed_cancel_stay() {
    let temp = tempfile::tempdir().unwrap();
    scan_all(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
    );
    let path = temp.path().join("tasks.jsonl");
    let mut all = load_tasks(&path).unwrap();
    let mut main = all.values().next().unwrap().clone();
    main.key = "plangraph:CI-FIX-main".into();
    main.pr = None;
    all.insert(main.key.clone(), main);
    save_tasks(&path, &all).unwrap();
    // Current head: nothing to cancel.
    let rows = sweep(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
        false,
        |_, _| panic!("cancelled a live item"),
    )
    .unwrap();
    assert!(rows.is_empty());
    // A refused cancel keeps the item for the next pass; main items are never touched.
    let rows = sweep(&mut World { prs: vec![] }, temp.path(), false, |_, _| {
        Err("keeper down".into())
    })
    .unwrap();
    assert!(
        rows.iter().any(|r| r.starts_with("cancel pending")),
        "{rows:?}"
    );
    assert_eq!(tasks(temp.path()).len(), 2);
    // A dry run changes nothing.
    let rows = sweep(&mut World { prs: vec![] }, temp.path(), true, |_, _| {
        panic!("dry")
    })
    .unwrap();
    assert!(rows[0].starts_with("would cancel"));
    assert_eq!(tasks(temp.path()).len(), 2);
}

fn pass(
    world: &mut World,
    dir: &Path,
    cancelled: &mut Vec<String>,
    keeper_up: bool,
) -> Vec<String> {
    scan_and_sweep(
        world,
        "o/r",
        dir,
        &BTreeMap::new(),
        false,
        |task| {
            task.keeper_key = Some(format!("lane-{}", task.run_id));
            Ok(true)
        },
        |_| Ok(false),
        |key, _| {
            if keeper_up {
                cancelled.push(key.to_owned());
                Ok(())
            } else {
                Err("keeper down".into())
            }
        },
    )
    .unwrap()
}

#[test]
fn a_new_head_failing_the_same_test_cancels_the_old_lane_and_files_a_fresh_one() {
    let temp = tempfile::tempdir().unwrap();
    let mut cancelled = Vec::new();
    pass(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
        &mut cancelled,
        true,
    );
    let old = tasks(temp.path()).remove(0);
    assert_eq!(
        old.keeper_key.as_deref(),
        Some(&*format!("lane-{}", run_id("s5")))
    );
    // The pull request moves on and the same test fails again: production order is sweep, then scan.
    pass(
        &mut World {
            prs: vec![live(5, "s5-new")],
        },
        temp.path(),
        &mut cancelled,
        true,
    );
    assert_eq!(cancelled, vec![old.keeper_key.clone().unwrap()]);
    let now = tasks(temp.path());
    assert_eq!(now.len(), 1);
    assert_eq!(now[0].sha.as_deref(), Some("s5-new"));
    assert_eq!(
        now[0].keeper_key.as_deref(),
        Some(&*format!("lane-{}", run_id("s5-new")))
    );
}

#[test]
fn a_refused_cancel_keeps_the_old_row_and_lane_instead_of_reusing_it_for_the_new_head() {
    let temp = tempfile::tempdir().unwrap();
    let mut cancelled = Vec::new();
    pass(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
        &mut cancelled,
        true,
    );
    let rows = pass(
        &mut World {
            prs: vec![live(5, "s5-new")],
        },
        temp.path(),
        &mut cancelled,
        false,
    );
    assert!(cancelled.is_empty());
    assert!(
        rows.iter().any(|r| r.starts_with("cancel pending")),
        "{rows:?}"
    );
    let now = tasks(temp.path());
    assert_eq!(now.len(), 1);
    assert_eq!(now[0].sha.as_deref(), Some("s5"));
    // The next pass with the keeper up cancels it and files the new head.
    pass(
        &mut World {
            prs: vec![live(5, "s5-new")],
        },
        temp.path(),
        &mut cancelled,
        true,
    );
    assert_eq!(cancelled.len(), 1);
    assert_eq!(tasks(temp.path())[0].sha.as_deref(), Some("s5-new"));
}

/// Answers `actions/runs/<id>` for the head the stored run belonged to.
struct Legacy {
    world: World,
    run: u64,
    head: &'static str,
}
impl Github for Legacy {
    fn pages(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>> {
        self.world.pages(e, f)
    }
    fn page(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>> {
        self.world.page(e, f)
    }
    fn log(&mut self, j: u64) -> Result<String> {
        self.world.log(j)
    }
    fn rerun(&mut self, r: u64) -> Result<()> {
        self.world.rerun(r)
    }
    fn value(&mut self, endpoint: &str) -> Result<Value> {
        assert_eq!(endpoint, format!("actions/runs/{}", self.run));
        Ok(json!({"id": self.run, "head_sha": self.head}))
    }
}

#[test]
fn a_row_without_a_stored_head_is_cancelled_when_its_run_belongs_to_an_older_head() {
    let temp = tempfile::tempdir().unwrap();
    scan_all(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
    );
    // Rewrite the row the way the format before the stored head wrote it.
    let path = temp.path().join("tasks.jsonl");
    let mut all = load_tasks(&path).unwrap();
    for task in all.values_mut() {
        task.sha = None;
        task.keeper_defined = true;
    }
    save_tasks(&path, &all).unwrap();
    let raw = std::fs::read_to_string(&path).unwrap();
    assert!(raw.contains("\"sha\":null"), "{raw}");
    let run = run_id("s5");
    // The same head: the row stays.
    let rows = sweep(
        &mut Legacy {
            world: World {
                prs: vec![live(5, "s5")],
            },
            run,
            head: "s5",
        },
        temp.path(),
        false,
        |_, _| panic!("cancelled a current item"),
    )
    .unwrap();
    assert!(rows.is_empty());
    // A moved head: it is cancelled.
    let mut cancelled = 0;
    sweep(
        &mut Legacy {
            world: World {
                prs: vec![live(5, "s5-new")],
            },
            run,
            head: "s5",
        },
        temp.path(),
        false,
        |_, _| {
            cancelled += 1;
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(cancelled, 1);
    assert!(tasks(temp.path()).is_empty());
}

#[test]
fn without_the_keeper_a_cancel_fails_so_the_row_is_kept() {
    assert!(cancel_item(false, &BTreeMap::new(), "plangraph:CI-FIX-x", "closed").is_err());
    let temp = tempfile::tempdir().unwrap();
    scan(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |_| Ok(true),
    )
    .unwrap();
    let rows = sweep(&mut World { prs: vec![] }, temp.path(), false, |k, r| {
        cancel_item(false, &BTreeMap::new(), k, r)
    })
    .unwrap();
    assert!(
        rows.iter().any(|r| r.starts_with("cancel pending")),
        "{rows:?}"
    );
    assert_eq!(tasks(temp.path()).len(), 1);
}

pub(super) fn train(number: u64, sha: &str) -> Value {
    pr(number, sha, "Landing train 33", false, "land/train-33")
}

/// A landing train's failure reaches the writer recorded for it, and files no fix item.
#[test]
fn a_failing_landing_train_goes_to_its_recorded_batch_and_files_no_fix_item() {
    let temp = tempfile::tempdir().unwrap();
    std::fs::write(
        temp.path().join("owners.json"),
        json!({"prs": {"7": "batch-a"}, "writers": {}}).to_string(),
    )
    .unwrap();
    let mut defined = 0;
    let rows = scan_and_sweep(
        &mut World {
            prs: vec![train(7, "t7"), train(8, "t8"), live(5, "s5")],
        },
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |_| {
            defined += 1;
            Ok(true)
        },
        |task| route_owned(temp.path(), task, 100),
        |_, _| Ok(()),
    )
    .unwrap();
    // Only the live pull request files a fix item; train 7 is recorded for its batch, train 8 has none.
    assert_eq!(defined, 1, "{rows:?}");
    assert_eq!(tasks(temp.path()).len(), 1);
    assert_eq!(tasks(temp.path())[0].pr, Some(5));
    let records = load_failures(temp.path()).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!((records[0].pr, records[0].batch.as_str()), (7, "batch-a"));
    assert!(
        rows.iter().any(|r| r.starts_with("routed train failure")),
        "{rows:?}"
    );
    assert!(
        rows.iter()
            .any(|r| r.starts_with("train failure has no recorded batch")),
        "{rows:?}"
    );
    // A second pass records nothing new and still files nothing for the trains.
    scan_and_sweep(
        &mut World {
            prs: vec![train(7, "t7"), train(8, "t8"), live(5, "s5")],
        },
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |_| Ok(true),
        |task| route_owned(temp.path(), task, 200),
        |_, _| Ok(()),
    )
    .unwrap();
    assert_eq!(load_failures(temp.path()).unwrap().len(), 1);
    assert_eq!(tasks(temp.path()).len(), 1);
}

#[test]
fn a_pending_cancel_of_a_row_without_a_stored_head_is_not_overwritten_by_the_new_head() {
    let temp = tempfile::tempdir().unwrap();
    let mut cancelled = Vec::new();
    pass(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
        &mut cancelled,
        true,
    );
    // The row as the format before the stored head wrote it.
    let path = temp.path().join("tasks.jsonl");
    let mut all = load_tasks(&path).unwrap();
    for task in all.values_mut() {
        task.sha = None;
    }
    save_tasks(&path, &all).unwrap();
    let old_run = tasks(temp.path())[0].run_id;
    // GitHub cannot name the old run's head and the keeper is down: the row must stay as it is.
    let rows = pass(
        &mut World {
            prs: vec![live(5, "s5-new")],
        },
        temp.path(),
        &mut cancelled,
        false,
    );
    assert!(
        rows.iter().any(|r| r.starts_with("cancel pending")),
        "{rows:?}"
    );
    let kept = tasks(temp.path());
    assert_eq!((kept.len(), kept[0].run_id), (1, old_run));
    assert_eq!(
        kept[0].keeper_key.as_deref(),
        Some(&*format!("lane-{old_run}"))
    );
}

#[test]
fn an_item_whose_create_reply_was_lost_is_cancelled_under_every_key_before_its_row_goes() {
    let temp = tempfile::tempdir().unwrap();
    // The keeper create succeeded but its reply was lost: the row is not defined and has no key.
    scan(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |_| Err("reply lost".into()),
    )
    .unwrap();
    let row = tasks(temp.path()).remove(0);
    assert!(!row.keeper_defined && row.keeper_key.is_none());
    let mut asked = Vec::new();
    let rows = sweep(&mut World { prs: vec![] }, temp.path(), false, |key, _| {
        asked.push(key.to_owned());
        // The successor lane is the one that exists and the keeper refuses it.
        if key.contains("-R") {
            Err("keeper down".into())
        } else {
            Ok(())
        }
    })
    .unwrap();
    assert_eq!(
        asked,
        [row.key.clone(), format!("{}-R{}", row.key, row.run_id)]
    );
    assert!(
        rows.iter().any(|r| r.starts_with("cancel pending")),
        "{rows:?}"
    );
    assert_eq!(
        tasks(temp.path()).len(),
        1,
        "row kept until every key is closed"
    );
    sweep(
        &mut World { prs: vec![] },
        temp.path(),
        false,
        |_, _| Ok(()),
    )
    .unwrap();
    assert!(tasks(temp.path()).is_empty());
}

/// A later run replaces the row of an item whose create reply was lost: the older successor key is
/// still closed when the pull request goes.
#[test]
fn an_older_successor_key_survives_a_later_run_replacing_the_row() {
    let temp = tempfile::tempdir().unwrap();
    let mut world = World {
        prs: vec![live(5, "s5")],
    };
    // The create for the successor lane of run 102 succeeded; its reply was lost.
    scan(
        &mut world,
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |task| {
            task.keeper_key = Some(format!("{}-R{}", task.key, task.run_id));
            Err("reply lost".into())
        },
    )
    .unwrap();
    let first = tasks(temp.path()).remove(0);
    assert_eq!(
        first.keeper_key.as_deref(),
        Some(&*format!("{}-R{}", first.key, first.run_id))
    );
    // The same head fails again in a newer run, replacing the row; delivery still has not succeeded.
    let mut row = tasks(temp.path()).remove(0);
    row.run_id += 50;
    let mut all = BTreeMap::new();
    all.insert(row.key.clone(), row.clone());
    save_tasks(&temp.path().join("tasks.jsonl"), &all).unwrap();
    let mut asked = Vec::new();
    sweep(&mut World { prs: vec![] }, temp.path(), false, |key, _| {
        asked.push(key.to_owned());
        Ok(())
    })
    .unwrap();
    assert!(
        asked.contains(&format!("{}-R{}", first.key, first.run_id)),
        "{asked:?}"
    );
}

#[test]
fn a_new_run_on_an_unchanged_head_does_not_leave_a_legacy_row_pending() {
    let temp = tempfile::tempdir().unwrap();
    let mut cancelled = Vec::new();
    pass(
        &mut World {
            prs: vec![live(5, "s5")],
        },
        temp.path(),
        &mut cancelled,
        true,
    );
    let path = temp.path().join("tasks.jsonl");
    let mut all = load_tasks(&path).unwrap();
    let old_run = tasks(temp.path())[0].run_id;
    for task in all.values_mut() {
        task.sha = None;
    }
    save_tasks(&path, &all).unwrap();
    // GitHub resolves the stored run to the head the pull request still has; a newer run fails on it.
    struct Same(World, u64);
    impl Github for Same {
        fn pages(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>> {
            self.0.pages(e, f)
        }
        fn page(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>> {
            let mut runs = self.0.page(e, f)?;
            for run in &mut runs {
                run["id"] = json!(self.1);
            }
            Ok(runs)
        }
        fn log(&mut self, j: u64) -> Result<String> {
            self.0.log(j)
        }
        fn rerun(&mut self, r: u64) -> Result<()> {
            self.0.rerun(r)
        }
        fn value(&mut self, _: &str) -> Result<Value> {
            Ok(json!({"head_sha": "s5"}))
        }
    }
    let rows = scan_and_sweep(
        &mut Same(
            World {
                prs: vec![live(5, "s5")],
            },
            old_run + 7,
        ),
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |task| {
            task.keeper_key = Some(format!("lane-{}", task.run_id));
            Ok(true)
        },
        |_| Ok(false),
        |_, _| panic!("the head did not move"),
    )
    .unwrap();
    assert!(
        !rows.iter().any(|r| r.starts_with("cancel pending")),
        "{rows:?}"
    );
    let now = tasks(temp.path());
    assert_eq!(
        (now[0].run_id, now[0].sha.as_deref()),
        (old_run + 7, Some("s5"))
    );
}

/// A train and an ordinary pull request on one commit each keep their own path.
#[test]
fn a_train_and_an_ordinary_pull_request_on_one_commit_are_both_handled() {
    let temp = tempfile::tempdir().unwrap();
    std::fs::write(
        temp.path().join("owners.json"),
        json!({"prs": {"5": "batch-a"}, "writers": {}}).to_string(),
    )
    .unwrap();
    let mut defined = Vec::new();
    // The train is #5 (owned), the ordinary pull request #6; both head commit shared.
    let rows = scan_and_sweep(
        &mut World {
            prs: vec![train(5, "shared"), live(6, "shared")],
        },
        "o/r",
        temp.path(),
        &BTreeMap::new(),
        false,
        |task| {
            defined.push(task.pr);
            Ok(true)
        },
        |task| route_owned(temp.path(), task, 100),
        |_, _| Ok(()),
    )
    .unwrap();
    assert_eq!(defined, [Some(6)], "{rows:?}");
    let records = load_failures(temp.path()).unwrap();
    assert_eq!(records.len(), 1, "{rows:?}");
    assert_eq!((records[0].pr, records[0].batch.as_str()), (5, "batch-a"));
}
