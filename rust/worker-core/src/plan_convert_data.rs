//! Input shape checks and reporting for the plan converter.
use super::{qualify, rows, text, trace};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

fn string_list(value: Option<&Value>, label: &str) -> Result<(), String> {
    if let Some(value) = value {
        if !value
            .as_array()
            .is_some_and(|a| a.iter().all(|v| v.as_str().is_some_and(|s| !s.is_empty())))
        {
            return Err(format!(
                "input: {label} must be a list of non-empty strings"
            ));
        }
    }
    Ok(())
}

pub(super) fn check(documents: &[Value], cards: &[Value]) -> Result<(), String> {
    for item in cards {
        string_list(item.get("depends_on"), "cards.depends_on")?;
    }
    for document in documents {
        let data = &document["data"];
        if !data.is_object() {
            return Err("input: plan data must be a mapping".into());
        }
        for section in [
            "items",
            "rewrite_dc",
            "reowner",
            "edit",
            "edits",
            "reedge",
            "hold",
            "unhold",
            "cancel",
            "merge",
            "reprio",
            "close",
            "decide",
            "bulk",
        ] {
            if data.get(section).is_some_and(|v| !v.is_array()) {
                return Err(format!("input: {section} must be a list"));
            }
            for row in rows(data, section) {
                if !row.is_object() {
                    return Err(format!("input: {section} row must be a mapping"));
                }
                if section != "bulk" && text(row, "id").is_empty() {
                    return Err(format!("input: {section} row needs id"));
                }
                let raw = row.get("fields").unwrap_or(row);
                if let Some(spec) = raw.get("depends_on") {
                    if spec.is_object() {
                        if spec.as_object().unwrap().is_empty()
                            || section == "items"
                            || spec
                                .as_object()
                                .unwrap()
                                .keys()
                                .any(|k| k != "add" && k != "remove")
                        {
                            return Err(
                                "input: depends_on supports only add/remove lists on edits".into(),
                            );
                        }
                        string_list(spec.get("add"), "depends_on.add")?;
                        string_list(spec.get("remove"), "depends_on.remove")?;
                    } else {
                        string_list(Some(spec), "depends_on")?;
                    }
                }
                for field in ["add", "drop"] {
                    string_list(row.get(field), field)?;
                }
            }
        }
    }
    Ok(())
}

pub(super) fn deferred(documents: &[Value]) -> Vec<Value> {
    let mut output = Vec::new();
    for document in documents {
        for section in ["close", "decide", "bulk"] {
            for row in rows(&document["data"], section) {
                if section == "bulk" && row.get("edges").is_some() {
                    continue;
                }
                output.push(json!({"source":document["path"], "section":section,
                    "row":row, "reason":"requires a separate evidence or planning workflow; no keeper operation generated"}));
            }
        }
    }
    output
}

pub(super) fn merge_notes(
    documents: &[Value],
    project: &str,
    old: &BTreeMap<String, Value>,
    creates: &BTreeMap<String, Value>,
    edits: &mut BTreeMap<String, Map<String, Value>>,
) -> Result<(), String> {
    for document in documents {
        for row in rows(&document["data"], "merge") {
            let source = qualify(project, &text(row, "id"));
            let target = qualify(project, &text(row, "into"));
            if source == target {
                return Err(format!("input: cannot merge {source} into itself"));
            }
            let survivor = old
                .get(&target)
                .or_else(|| creates.get(&target))
                .ok_or_else(|| format!("not_found: merge survivor {target}"))?;
            let title = old
                .get(&source)
                .map(|v| text(v, "title"))
                .unwrap_or_default();
            let note = json!({"note":format!("Absorbs {source} ({title}): {}",text(row,"reason"))});
            trace(
                edits.entry(target).or_default(),
                survivor,
                &format!("{}#merge", text(document, "path")),
                &note,
            );
        }
    }
    Ok(())
}
