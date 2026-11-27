//! The feeder starts writers only for items with an accepted spec. The spec card
//! `docs/specs/<ID>.card.yaml` (JSON) on the checkout names the write set the writer gets; an
//! item without one waits until the planning session has written its spec.
use serde_json::{json, Value};
use std::path::Path;

/// The write set of `key`'s spec card in `repo`, or `None` when the item has no usable spec.
pub fn write_set(repo: &Path, key: &str) -> Option<Vec<String>> {
    let id = key.split_once(':')?.1;
    if id.is_empty() || id.contains('/') || id.contains("..") {
        return None;
    }
    let path = repo.join("docs/specs").join(format!("{id}.card.yaml"));
    let card: Value = serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()?;
    let files: Vec<String> = card["cards"][id]["write_set"]
        .as_array()?
        .iter()
        .filter_map(|v| v.as_str().map(str::to_owned))
        .collect();
    (!files.is_empty()).then_some(files)
}

/// `ready` with only the items that have a spec, each carrying its spec's write set as `files`,
/// and the keys left out for want of a spec.
pub fn require_specs(repo: &Path, ready: &Value) -> (Value, Vec<String>) {
    let mut kept = ready.clone();
    let mut missing = Vec::new();
    if let Some(items) = kept["items"].as_array_mut() {
        items.retain_mut(|item| {
            let key = item["key"].as_str().unwrap_or("").to_owned();
            match write_set(repo, &key) {
                Some(files) => {
                    item["files"] = json!(files);
                    true
                }
                None => {
                    missing.push(key);
                    false
                }
            }
        });
    }
    (kept, missing)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn card(dir: &Path, id: &str, body: &str) {
        std::fs::create_dir_all(dir.join("docs/specs")).unwrap();
        std::fs::write(dir.join(format!("docs/specs/{id}.card.yaml")), body).unwrap();
    }

    #[test]
    fn only_items_with_a_spec_card_and_a_write_set_reach_the_feeder() {
        let dir = tempfile::tempdir().unwrap();
        card(
            dir.path(),
            "WITH-SPEC",
            r#"{"project":"plangraph","cards":{"WITH-SPEC":{"write_set":["src/a.rs","changelog.d/WITH-SPEC.md"]}}}"#,
        );
        card(
            dir.path(),
            "EMPTY-SET",
            r#"{"cards":{"EMPTY-SET":{"write_set":[]}}}"#,
        );
        card(dir.path(), "BROKEN", "not json");
        let ready = json!({"items":[
            {"key":"plangraph:WITH-SPEC","files":["ignored.rs"]},
            {"key":"plangraph:NO-SPEC"},
            {"key":"plangraph:EMPTY-SET"},
            {"key":"plangraph:BROKEN"},
            {"key":"plangraph:../escape"}]});
        let (kept, missing) = require_specs(dir.path(), &ready);
        assert_eq!(
            kept["items"],
            json!([{"key":"plangraph:WITH-SPEC","files":["src/a.rs","changelog.d/WITH-SPEC.md"]}])
        );
        assert_eq!(
            missing,
            [
                "plangraph:NO-SPEC",
                "plangraph:EMPTY-SET",
                "plangraph:BROKEN",
                "plangraph:../escape"
            ]
        );
    }
}
