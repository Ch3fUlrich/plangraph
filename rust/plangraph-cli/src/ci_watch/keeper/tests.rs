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
        class: "code".into(),
        run_class: "code".into(),
        job_id: 4242,
        head_sha: String::new(),
        current: false,
    }
}
#[test]
fn keeper_definition_names_the_class_job_and_where_to_start() {
    let mut task = task();
    task.class = "infrastructure".into();
    task.run_class = "mixed".into();
    let item = definition(&task, &task.key);
    let text = item["description"].as_str().unwrap();
    assert!(
        text.contains("Class: infrastructure (run: mixed)"),
        "{text}"
    );
    assert!(text.contains("Failed job id: 4242"), "{text}");
    assert!(text.contains("not the batch's code"), "{text}");
    assert!(text.contains("timestamp context"), "{text}");
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
