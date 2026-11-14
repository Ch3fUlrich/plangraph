//! Deterministic plan-data to atomic keeper operations; keeper owns validation.
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
#[path = "plan_convert_data.rs"]
mod data;

fn text(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or("").to_owned()
}
fn qualify(project: &str, id: &str) -> String {
    if id.contains(':') {
        id.to_owned()
    } else {
        format!("{project}:{id}")
    }
}
fn rows<'a>(v: &'a Value, key: &str) -> impl Iterator<Item = &'a Value> {
    v.get(key).and_then(Value::as_array).into_iter().flatten()
}
fn deps(v: &Value, project: &str) -> BTreeSet<String> {
    v.as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(|d| qualify(project, d))
        .collect()
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn copy_fields(row: &Value, fields: &mut Map<String, Value>, names: &[&str]) {
    for name in names {
        if let Some(value) = row.get(*name).filter(|v| !v.is_null()) {
            fields.insert((*name).to_owned(), value.clone());
        }
    }
    if let Some(value) = row.get("est_h").filter(|v| !v.is_null()) {
        fields.insert("estimate_h".into(), value.clone());
        fields.insert("estimate_src".into(), json!("plan-convert"));
    }
}
fn trace(fields: &mut Map<String, Value>, old: &Value, source: &str, row: &Value) {
    let previous = fields
        .get("note")
        .or_else(|| old.get("note"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let line = format!(
        "plan-convert: {source}{}",
        row.get("note")
            .and_then(Value::as_str)
            .map(|n| format!(": {n}"))
            .unwrap_or_default()
    );
    fields.insert(
        "note".into(),
        json!(if previous.is_empty() {
            line
        } else {
            format!("{previous}\n{line}")
        }),
    );
}

/// Input: decoded YAML documents, actual input paths, a cards snapshot and options.
/// Output contains a batch and manifest. No writes occur here.
pub fn convert(request: &Value) -> Result<Value, String> {
    let project = text(request, "project");
    if project.is_empty() {
        return Err("input: project is required".into());
    }
    let snapshot = &request["snapshot"];
    let cards = snapshot
        .get("items")
        .and_then(Value::as_array)
        .ok_or("input: cards must contain an items list")?;
    let mut old: BTreeMap<String, Value> = BTreeMap::new();
    for item in cards {
        let key = if text(item, "key").is_empty() {
            qualify(&text(item, "project"), &text(item, "id"))
        } else {
            text(item, "key")
        };
        if old.insert(key.clone(), item.clone()).is_some() {
            return Err(format!("input: duplicate card {key}"));
        }
    }
    let mut creates = BTreeMap::<String, Value>::new();
    let mut edits = BTreeMap::<String, Map<String, Value>>::new();
    let mut cancels = BTreeMap::<String, String>::new();
    let mut priorities = BTreeMap::<String, Value>::new();
    let documents = request["documents"]
        .as_array()
        .ok_or("input: documents must be a list")?;
    data::check(documents, cards)?;
    for doc in documents {
        let source = text(doc, "path");
        let data = &doc["data"];
        for raw in rows(data, "items") {
            let id = text(raw, "id");
            if id.is_empty() {
                return Err(format!("input: {source}: item needs id"));
            }
            let p = raw
                .get("project")
                .and_then(Value::as_str)
                .unwrap_or(&project);
            let key = qualify(p, &id);
            if old.contains_key(&key) || creates.contains_key(&key) {
                return Err(format!("conflict: create {key} already exists"));
            }
            let mut item = Map::new();
            copy_fields(
                raw,
                &mut item,
                &[
                    "title",
                    "kind",
                    "track",
                    "owner",
                    "priority",
                    "estimate_h",
                    "estimate_src",
                    "done_criteria",
                    "evidence_kinds",
                    "review_tier",
                    "description",
                    "operator",
                    "note",
                    "host",
                    "wait",
                    "choices",
                    "risk_tag",
                ],
            );
            item.insert("id".into(), json!(id));
            item.insert("project".into(), json!(p));
            item.insert("depends_on".into(), json!(deps(&raw["depends_on"], p)));
            if !item.contains_key("description") {
                if let Some(why) = raw.get("why").and_then(Value::as_str) {
                    item.insert(
                        "description".into(),
                        json!(format!("Why: {why}\nPlan: {source}#{id}")),
                    );
                }
            }
            if let Some(parent) = raw.get("under").and_then(Value::as_str) {
                let parent_key = qualify(p, parent);
                let parent_old = old.get(&parent_key).cloned().unwrap_or(Value::Null);
                let field = edits.entry(parent_key.clone()).or_default();
                let mut edges = deps(
                    field.get("depends_on").unwrap_or(&parent_old["depends_on"]),
                    p,
                );
                edges.insert(key.clone());
                field.insert("depends_on".into(), json!(edges));
                if !item.contains_key("track") {
                    if let Some(track) = parent_old.get("track").filter(|v| !v.is_null()) {
                        item.insert("track".into(), track.clone());
                    }
                }
            }
            creates.insert(key, Value::Object(item));
        }
        for section in ["rewrite_dc", "reowner", "edit", "edits"] {
            for row in rows(data, section) {
                let key = qualify(&project, &text(row, "id"));
                let fields = edits.entry(key.clone()).or_default();
                let raw = row.get("fields").unwrap_or(row);
                copy_fields(
                    raw,
                    fields,
                    &[
                        "title",
                        "done_criteria",
                        "estimate_h",
                        "estimate_src",
                        "owner",
                        "track",
                        "review_tier",
                        "description",
                        "evidence_kinds",
                    ],
                );
                if let Some(criteria) = raw.get("criteria") {
                    fields.insert("done_criteria".into(), criteria.clone());
                }
                if let Some(priority) = raw.get("priority") {
                    priorities.insert(key.clone(), priority.clone());
                }
                if !["done_criteria", "criteria", "title", "replace"]
                    .iter()
                    .any(|k| raw.get(k).is_some_and(|v| !v.is_null()))
                {
                    copy_fields(raw, fields, &["note"]);
                }
                for replacement in rows(raw, "replace") {
                    let previous = fields
                        .get("done_criteria")
                        .or_else(|| old.get(&key).and_then(|v| v.get("done_criteria")))
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let needle = text(replacement, "old");
                    if needle.is_empty() || previous.matches(&needle).count() != 1 {
                        return Err(format!(
                            "input: {key}: criteria replacement must match exactly once"
                        ));
                    }
                    fields.insert(
                        "done_criteria".into(),
                        json!(previous.replacen(&needle, &text(replacement, "new"), 1)),
                    );
                }
                if let Some(spec) = raw.get("depends_on") {
                    let previous = old.get(&key).unwrap_or(&Value::Null);
                    let mut edges = deps(
                        fields.get("depends_on").unwrap_or(&previous["depends_on"]),
                        &project,
                    );
                    if spec.is_array() {
                        edges = deps(spec, &project);
                    } else {
                        for edge in deps(&spec["remove"], &project) {
                            edges.remove(&edge);
                        }
                        edges.extend(deps(&spec["add"], &project));
                    }
                    fields.insert("depends_on".into(), json!(edges));
                }
                if fields.contains_key("done_criteria")
                    || raw.get("title").is_some_and(|v| !v.is_null())
                {
                    trace(
                        fields,
                        old.get(&key).unwrap_or(&Value::Null),
                        &format!("{source}#{section}"),
                        row,
                    );
                }
            }
        }
        for section in ["reedge", "hold", "unhold"] {
            for row in rows(data, section) {
                let key = qualify(&project, &text(row, "id"));
                let fields = edits.entry(key.clone()).or_default();
                let before = old.get(&key).unwrap_or(&Value::Null);
                let mut edges = deps(
                    fields.get("depends_on").unwrap_or(&before["depends_on"]),
                    &project,
                );
                for edge in deps(&row["drop"], &project) {
                    edges.remove(&edge);
                }
                for edge in deps(&row["add"], &project) {
                    edges.insert(edge);
                }
                if let Some(blocker) = row.get("blocker").and_then(Value::as_str) {
                    let blocker = qualify(&project, blocker);
                    if section == "unhold" {
                        edges.remove(&blocker);
                    } else {
                        edges.insert(blocker);
                    }
                }
                fields.insert("depends_on".into(), json!(edges));
            }
        }
        for section in ["cancel", "merge"] {
            for row in rows(data, section) {
                let key = qualify(&project, &text(row, "id"));
                let mut reason = text(row, "reason");
                if let Some(into) = row.get("into").and_then(Value::as_str) {
                    reason = format!("{reason}; merged into {}", qualify(&project, into));
                }
                if cancels.insert(key.clone(), reason).is_some() {
                    return Err(format!("input: duplicate cancellation {key}"));
                }
            }
        }
        for row in rows(data, "reprio") {
            priorities.insert(qualify(&project, &text(row, "id")), row["priority"].clone());
        }
        for row in rows(data, "bulk") {
            for edge in rows(row, "edges") {
                let parts = edge.as_array().ok_or("input: bulk edge must be a list")?;
                if parts.len() != 4 {
                    return Err("input: bulk edge needs source, target, action, replacement".into());
                }
                let key = qualify(
                    &project,
                    parts[0]
                        .as_str()
                        .ok_or("input: bulk edge source must be text")?,
                );
                let fields = edits.entry(key.clone()).or_default();
                let before = old.get(&key).unwrap_or(&Value::Null);
                let mut edges = deps(
                    fields.get("depends_on").unwrap_or(&before["depends_on"]),
                    &project,
                );
                edges.remove(&qualify(
                    &project,
                    parts[1]
                        .as_str()
                        .ok_or("input: bulk edge target must be text")?,
                ));
                if parts[2].as_str() == Some("redirect") {
                    let target = qualify(
                        &project,
                        parts[3]
                            .as_str()
                            .ok_or("input: redirect needs replacement")?,
                    );
                    if target != key {
                        edges.insert(target);
                    }
                } else if !parts[2].as_str().unwrap_or("").starts_with("drop") {
                    return Err("input: unsupported bulk edge action".into());
                }
                fields.insert("depends_on".into(), json!(edges));
            }
        }
    }
    data::merge_notes(documents, &project, &old, &creates, &mut edits)?;
    // A new parent is part of the single create list, never a second edit.
    let created_keys: Vec<_> = creates.keys().cloned().collect();
    for key in created_keys {
        if let Some(fields) = edits.remove(&key) {
            creates
                .get_mut(&key)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .extend(fields);
        }
        if let Some(priority) = priorities.remove(&key) {
            creates.get_mut(&key).unwrap()["priority"] = priority;
        }
    }
    for key in edits.keys().chain(cancels.keys()).chain(priorities.keys()) {
        if !old.contains_key(key) {
            return Err(format!("not_found: existing item {key} missing from cards"));
        }
    }
    let mut ops = Vec::new();
    if !creates.is_empty() {
        ops.push(json!({"op":"create", "item":creates.values().collect::<Vec<_>>()}));
    }
    for (key, fields) in edits {
        if !fields.is_empty() {
            ops.push(json!({"op":"edit", "key":key, "fields":fields}));
        }
    }
    for (key, reason) in cancels {
        ops.push(json!({"op":"cancel", "key":key, "reason":reason}));
    }
    for (key, priority) in priorities {
        ops.push(json!({"op":"priority", "key":key, "priority":priority}));
    }
    let mut inputs = Vec::new();
    for path in request["input_files"]
        .as_array()
        .ok_or("input: input_files missing")?
    {
        let path = path.as_str().ok_or("input: input path must be text")?;
        let bytes = fs::read(path).map_err(|e| format!("input: {path}: {e}"))?;
        if request["input_hashes"][path].as_str() != Some(&digest(&bytes)) {
            return Err(format!("input_changed: {path} changed during conversion"));
        }
        inputs.push(json!({"path":path, "sha256":digest(&bytes)}));
    }
    let manifest = json!({"format":1, "inputs": inputs, "not_converted":data::deferred(documents),
        "ordered_ops_sha256":digest(&serde_json::to_vec(&ops).unwrap())});
    let batch = json!({"batch":request["batch"], "principal": request["principal"],
        "source":"manifest.json", "source_sha256":digest(&serde_json::to_vec(&manifest).unwrap()),
        "base_seq": request["base_seq"], "ops": ops});
    Ok(json!({"batch": batch, "manifest":manifest}))
}
