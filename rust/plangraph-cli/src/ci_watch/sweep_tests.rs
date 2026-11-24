use super::github::Github;
use super::sweep::{not_a_fix_lane_pr, sweep};
use super::{load_tasks, save_tasks, scan, Result, Task};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// Open pull requests plus one failed run on each head; a PR missing from `prs` is closed.
struct World {
    prs: Vec<Value>,
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
                runs.push(json!({"id": 7, "head_sha": sha, "event": "pull_request",
                    "head_branch": "x", "conclusion": "failure"}));
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

fn pr(number: u64, sha: &str, title: &str, draft: bool, branch: &str) -> Value {
    json!({"number": number, "title": title, "draft": draft,
        "head": {"sha": sha, "ref": branch}})
}
fn live(number: u64, sha: &str) -> Value {
    pr(number, sha, "Fix the thing", false, "lane/thing")
}
fn tasks(dir: &Path) -> Vec<Task> {
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
