use super::*;
use serde_json::json;
#[derive(Default)]
struct Recorded {
    items: BTreeMap<String, Value>,
    creates: Vec<Value>,
    lose_reply: bool,
}
impl Keeper for Recorded {
    fn show(&mut self, key: &str) -> Result<Option<Value>> {
        Ok(self.items.get(key).cloned())
    }
    fn create(&mut self, item: Value) -> Result<()> {
        self.creates.push(item.clone());
        self.items.insert(
            format!("plangraph:{}", item["id"].as_str().unwrap()),
            json!({"state":"open","description":item["description"]}),
        );
        if self.lose_reply {
            Err("lost reply".into())
        } else {
            Ok(())
        }
    }
}
fn task() -> Task {
    Task {
        key: "plangraph:CI-FIX-example".into(),
        head: "main".into(),
        pr: None,
        run_id: 900,
        job: "pytest".into(),
        test: ["tests", "test_credential_guard.py::test_x"].join("/"),
        excerpt: "timestamp context\n".repeat(40),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
        sha: None,
    }
}
#[test]
fn keeper_definition_obeys_caps_guard_and_default_tier() {
    let mut task = task();
    task.test.push_str(&"long界".repeat(100));
    task.excerpt = "timestamp 界 context\n".repeat(400);
    let item = definition(&task, &task.key);
    assert!(item["title"].as_str().unwrap().chars().count() <= 200);
    let desc = item["description"].as_str().unwrap();
    assert!(desc.chars().count() <= 2000);
    assert!(desc.contains("Why:"));
    assert!(desc.contains("How:"));
    assert!(!item["title"]
        .as_str()
        .unwrap()
        .contains(&["tests", "test_credential_guard"].join("/")));
    assert_eq!(contract_text(desc, 2000), desc);
    assert!(item.get("review_tier").is_none());
    assert_eq!(item["priority"], 10);
    assert_eq!(item["under"], PARENT);
}
#[test]
fn closed_card_gets_one_successor_and_lost_reply_is_idempotent() {
    let mut task = task();
    let mut keeper = Recorded::default();
    keeper.items.insert(PARENT.into(), json!({"state":"open"}));
    keeper.items.insert(
        task.key.clone(),
        json!({"state":"done","description":"[ci-watch-run:899]"}),
    );
    keeper.lose_reply = true;
    assert!(deliver(&mut keeper, &mut task).is_err());
    keeper.lose_reply = false;
    assert!(deliver(&mut keeper, &mut task).unwrap());
    assert_eq!(keeper.creates.len(), 1);
    assert_eq!(
        task.keeper_key.as_deref(),
        Some("plangraph:CI-FIX-example-R900")
    );
    assert!(deliver(&mut keeper, &mut task).unwrap());
    assert_eq!(keeper.creates.len(), 1);
    keeper.items.insert(
        task.keeper_key.clone().unwrap(),
        json!({"state":"done","description":"[ci-watch-run:900]"}),
    );
    assert!(deliver(&mut keeper, &mut task).unwrap());
    assert_eq!(keeper.creates.len(), 1);
    task.run_id = 901;
    assert!(deliver(&mut keeper, &mut task).unwrap());
    assert_eq!(keeper.creates.len(), 2);
}
#[test]
fn active_card_is_reused_and_closed_parent_is_reported() {
    let mut task = task();
    let mut keeper = Recorded::default();
    keeper
        .items
        .insert(task.key.clone(), json!({"state":"claimed"}));
    assert!(deliver(&mut keeper, &mut task).unwrap());
    assert!(keeper.creates.is_empty());
    keeper.items.clear();
    keeper.items.insert(PARENT.into(), json!({"state":"done"}));
    assert!(deliver(&mut keeper, &mut task)
        .unwrap_err()
        .contains("must be active"));
    assert!(keeper.creates.is_empty());
}
#[test]
fn a_create_whose_reply_is_lost_leaves_the_attempted_key_on_the_row() {
    let mut task = task();
    let mut keeper = Recorded::default();
    keeper.items.insert(PARENT.into(), json!({"state":"open"}));
    keeper.items.insert(
        task.key.clone(),
        json!({"state":"done","description":"[ci-watch-run:899]"}),
    );
    keeper.lose_reply = true;
    assert!(deliver(&mut keeper, &mut task).is_err());
    // The item exists under the successor key and the row still names it.
    assert!(keeper.items.contains_key("plangraph:CI-FIX-example-R900"));
    assert_eq!(
        task.keeper_key.as_deref(),
        Some("plangraph:CI-FIX-example-R900")
    );
}

#[test]
fn the_selected_key_is_on_disk_when_the_create_is_issued() {
    /// Reads the row file at the moment of the create.
    struct Watching {
        inner: Recorded,
        path: std::path::PathBuf,
        on_disk_at_create: Option<Option<String>>,
    }
    impl Keeper for Watching {
        fn show(&mut self, key: &str) -> Result<Option<Value>> {
            self.inner.show(key)
        }
        fn create(&mut self, item: Value) -> Result<()> {
            self.on_disk_at_create = Some(
                super::super::load_tasks(&self.path)
                    .unwrap()
                    .values()
                    .next()
                    .and_then(|row| row.keeper_key.clone()),
            );
            self.inner.create(item)
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tasks.jsonl");
    let mut task = task();
    // The row exists without a key, as the scan saves it before it defines.
    let mut rows = BTreeMap::new();
    rows.insert(task.key.clone(), task.clone());
    super::super::save_tasks(&path, &rows).unwrap();
    let mut keeper = Watching {
        inner: Recorded::default(),
        path: path.clone(),
        on_disk_at_create: None,
    };
    keeper
        .inner
        .items
        .insert(PARENT.into(), json!({"state":"open"}));
    keeper.inner.items.insert(
        task.key.clone(),
        json!({"state":"done","description":"[ci-watch-run:899]"}),
    );
    assert!(deliver_with(&mut keeper, &mut task, &mut |t| persist_row(&path, t)).unwrap());
    assert_eq!(
        keeper.on_disk_at_create,
        Some(Some("plangraph:CI-FIX-example-R900".to_owned())),
        "the key was only in memory when the create went out"
    );
}
