//! A feeder writer starts only with its spec card's write set as files and a lane branch.
use plangraph_cli::feeder::*;
use serde_json::{json, Value};

#[derive(Default)]
struct Fake {
    calls: Vec<String>,
    cards: Vec<Value>,
    briefs: Vec<String>,
}
impl Launch for Fake {
    fn claim(&mut self, key: &str) -> Result<u64, ClaimError> {
        self.calls.push(format!("claim {key}"));
        Ok(3)
    }
    fn start(&mut self, card: &Value, brief: &str) -> Result<Value, String> {
        self.cards.push(card.clone());
        self.briefs.push(brief.into());
        Ok(json!({"run_id":"run-ok","status":"running"}))
    }
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String> {
        self.calls.push(format!("release {key} {epoch}"));
        Ok(())
    }
}

fn facts() -> HostFacts {
    serde_json::from_value(json!({"host":"dell","free_ram_bytes":8000000000u64,
        "free_disk_bytes":20000000000u64,"quota_remaining_percent":80,"observed_at":1000,"now":1001}))
    .unwrap()
}

fn item(key: &str, priority: i64) -> Value {
    json!({"key":key,"title":key,"priority":priority,"state":"open","epoch":0,"files":[]})
}

#[test]
fn only_an_item_with_a_spec_write_set_is_claimed_and_its_card_carries_files_and_a_lane() {
    let repo = tempfile::tempdir().unwrap();
    let specs = repo.path().join("docs/specs");
    std::fs::create_dir_all(&specs).unwrap();
    let write_set = ["rust/a.rs", "rust/a_tests.rs"];
    std::fs::write(
        specs.join("WITH-SPEC.card.yaml"),
        json!({"project":"plangraph","cards":{"WITH-SPEC":{"write_set":write_set}}}).to_string(),
    )
    .unwrap();
    std::fs::write(
        specs.join("EMPTY-SPEC.card.yaml"),
        json!({"project":"plangraph","cards":{"EMPTY-SPEC":{"write_set":[]}}}).to_string(),
    )
    .unwrap();
    let ready = json!({"items":[item("plangraph:NO-SPEC", 9), item("plangraph:EMPTY-SPEC", 8),
        item("plangraph:WITH-SPEC", 1)]});

    let (admitted, skipped) = with_spec_write_sets(&ready, repo.path());
    let reasons: Vec<(&str, &str)> = skipped
        .iter()
        .map(|row| {
            (
                row["item_key"].as_str().unwrap(),
                row["reason"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        reasons,
        [
            ("plangraph:NO-SPEC", "no_spec"),
            (
                "plangraph:EMPTY-SPEC",
                "no_spec: spec card names no write_set"
            )
        ]
    );

    let mut fake = Fake::default();
    let row = tick(
        &admitted,
        &[],
        &facts(),
        &Policy::default(),
        "rules",
        "/repo",
        "/reviews",
        false,
        &mut fake,
    );
    assert_eq!(row["item_key"], "plangraph:WITH-SPEC", "{row}");
    assert_eq!(fake.calls, ["claim plangraph:WITH-SPEC"]);
    assert_eq!(fake.cards[0]["files"], json!(write_set));
    assert_eq!(
        fake.cards[0]["push_ref"],
        "refs/heads/lane/feeder-with-spec"
    );
    assert!(fake.briefs[0].contains("Files:\nrust/a.rs\nrust/a_tests.rs"));
    assert!(fake.briefs[0].contains("Lane branch: lane/feeder-with-spec"));
}
