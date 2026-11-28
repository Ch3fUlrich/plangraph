//! Spec admission: a feeder writer gets the write set of the item's spec card as its files.
use serde_json::{json, Value};
use std::path::Path;

/// The ready list with only the items whose spec card (`docs/specs/<KEY>.card.yaml` in `repo`)
/// names a write set, each with `files` set to that write set, and a `no_spec` skip row for every
/// other item, so no writer is ever started without an editable file list.
pub fn with_spec_write_sets(ready: &Value, repo: &Path) -> (Value, Vec<Value>) {
    let mut admitted = ready.clone();
    let mut skipped = Vec::new();
    if let Some(items) = admitted["items"].as_array_mut() {
        items.retain_mut(
            |item| match write_set(repo, item["key"].as_str().unwrap_or("")) {
                Ok(files) => {
                    item["files"] = json!(files);
                    true
                }
                Err(reason) => {
                    skipped.push(json!({"item_key":item["key"],"reason":reason}));
                    false
                }
            },
        );
    }
    (admitted, skipped)
}

/// The write set of the spec card of `key` (`project:KEY`); the card file is JSON, which YAML reads too.
fn write_set(repo: &Path, key: &str) -> Result<Vec<String>, String> {
    let bare = key.rsplit(':').next().unwrap_or_default();
    let safe = |b: u8| b.is_ascii_alphanumeric() || b == b'-' || b == b'_';
    if bare.is_empty() || !bare.bytes().all(safe) {
        return Err("no_spec: item key cannot name a spec card".into());
    }
    let path = repo.join("docs/specs").join(format!("{bare}.card.yaml"));
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err("no_spec".into()),
        Err(e) => return Err(format!("no_spec: spec card unreadable: {e}")),
    };
    let card: Value =
        serde_json::from_str(&text).map_err(|e| format!("no_spec: spec card is not JSON: {e}"))?;
    let files: Vec<String> = card["cards"][bare]["write_set"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|file| file.as_str().filter(|f| !f.is_empty()).map(str::to_owned))
        .collect();
    if files.is_empty() {
        return Err("no_spec: spec card names no write_set".into());
    }
    Ok(files)
}
