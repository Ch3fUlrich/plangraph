use super::keeper::persist_row;
use super::{load_tasks, store_task, update_tasks, Task};
use std::collections::BTreeMap;

fn row(key: &str) -> Task {
    Task {
        key: key.into(),
        head: "main".into(),
        pr: None,
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
        sha: None,
    }
}

/// Passes that hold different locks all change the ledger: no change may erase another.
#[test]
fn concurrent_ledger_updates_never_erase_each_other() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.jsonl");
    std::thread::scope(|scope| {
        for n in 0..12 {
            let path = &path;
            scope.spawn(move || {
                let key = format!("plangraph:CI-FIX-{n}");
                if n % 2 == 0 {
                    update_tasks(path, |rows| {
                        rows.insert(key.clone(), row(&key));
                    })
                    .unwrap();
                } else {
                    persist_row(path, &row(&key)).unwrap();
                }
            });
        }
    });
    assert_eq!(load_tasks(&path).unwrap().len(), 12);
}

/// The scan saves its row with the keeper key after `define` returns; a row another pass wrote in the
/// meantime (main-freeze, under its own lock) stays, and so does the key the checkpoint wrote.
#[test]
fn a_row_written_during_define_survives_the_scans_final_save() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.jsonl");
    let mut tasks = BTreeMap::new();
    let mut output = Vec::new();
    let mut define = |task: &mut Task| {
        task.keeper_key = Some("plangraph:CI-FIX-a-R1".into());
        persist_row(&path, task)?;
        // Another pass adds its own row while the keeper call is out.
        update_tasks(&path, |rows| {
            rows.insert(
                "plangraph:CI-FIX-other".into(),
                row("plangraph:CI-FIX-other"),
            );
        })?;
        Ok(true)
    };
    store_task(
        &path,
        &mut tasks,
        row("plangraph:CI-FIX-a"),
        &mut define,
        &mut output,
    )
    .unwrap();
    let saved = load_tasks(&path).unwrap();
    assert_eq!(saved.len(), 2, "the other pass's row was erased");
    let mine = &saved["plangraph:CI-FIX-a"];
    assert!(mine.keeper_defined);
    assert_eq!(mine.keeper_key.as_deref(), Some("plangraph:CI-FIX-a-R1"));
}
