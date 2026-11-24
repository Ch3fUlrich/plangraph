use super::*;
use std::collections::BTreeSet;

/// Current keeper folds unknown create fields into `note` as `name: JSON/value`.
/// Decode only explicit metadata labels; never infer scope or PRs from prose.
pub fn decode_task(v: &Value) -> Result<Task, String> {
    let mut v = v.clone();
    if !v.is_object() {
        return Err("invalid keeper item".into());
    }
    if v.get("note")
        .is_some_and(|n| !n.is_null() && !n.is_string())
    {
        return Err("invalid keeper note".into());
    }
    if v.get("context")
        .is_some_and(|c| !c.is_null() && !c.is_object())
    {
        return Err("invalid keeper context".into());
    }
    let explicit: BTreeSet<String> = v
        .as_object()
        .expect("checked object")
        .iter()
        .filter(|(_, v)| !v.is_null())
        .map(|(k, _)| k.clone())
        .collect();
    let note = v["note"].as_str().unwrap_or("").to_string();
    for line in note.lines() {
        let Some((name, value)) = line.split_once(": ") else {
            continue;
        };
        if !matches!(
            name,
            "files"
                | "pr_state"
                | "pr"
                | "pr_url"
                | "pr_number"
                | "pull_request"
                | "open_pr"
                | "ci_fix"
                | "in_merge_batch"
                | "merge_batch"
        ) {
            continue;
        }
        if explicit.contains(name) {
            continue;
        }
        v[name] = serde_json::from_str(value).unwrap_or_else(|_| json!(value));
    }
    if let Some(path) = v["files"].as_str() {
        v["files"] = if path.is_empty() {
            json!([])
        } else {
            json!([path])
        };
    }
    let task: Task = serde_json::from_value(v).map_err(|_| "invalid keeper item metadata")?;
    if task.key.is_empty() || !task.key.contains(':') {
        return Err("invalid keeper item key".into());
    }
    Ok(task)
}
pub fn validate_ready(ready: &Value) -> Result<(), String> {
    ready["items"].as_array().ok_or("ready missing items")?;
    for group in ["needs_reconcile", "in_progress"] {
        if let Some(v) = ready.get(group) {
            for v in v.as_array().ok_or("invalid ready exclusion group")? {
                if v.as_str().or_else(|| v["key"].as_str()).is_none() {
                    return Err("invalid ready exclusion key".into());
                }
            }
        }
    }
    Ok(())
}
/// Bad candidates are isolated and audited; invalid running-writer scope still
/// refuses in the source adapter. A malformed envelope cannot be interpreted.
pub fn prepare_ready(ready: &Value) -> Result<(Value, Vec<Value>), String> {
    validate_ready(ready)?;
    let mut prepared = ready.clone();
    let mut skipped = Vec::new();
    prepared["items"]
        .as_array_mut()
        .expect("validated items")
        .retain(|v| match decode_task(v) {
            Ok(_) => true,
            Err(reason) => {
                skipped.push(json!({"item_key":v["key"],"reason":reason}));
                false
            }
        });
    Ok((prepared, skipped))
}
fn family(key: &str) -> String {
    let (project, bare) = key.split_once(':').unwrap_or((key, ""));
    format!("{project}:{}", bare.split('-').next().unwrap_or(bare)).to_ascii_uppercase()
}
fn overlap(a: &Task, b: &Run) -> bool {
    if a.key.split_once(':').map(|x| x.0) != b.item_key.split_once(':').map(|x| x.0) {
        return false;
    }
    let mine = family(&a.key);
    mine == family(&b.item_key)
        || b.item_keys.iter().any(|k| mine == family(k))
        || a.files
            .iter()
            .any(|a| b.files.iter().any(|b| crate::paths::paths_overlap(a, b)))
}
fn signal(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_u64().is_some_and(|n| n > 0),
        Value::String(s) => {
            !s.trim().is_empty()
                && !matches!(
                    s.trim().to_ascii_lowercase().as_str(),
                    "false" | "0" | "null" | "none" | "no" | "n/a" | "n-a" | "nil"
                )
        }
        Value::Object(m)
            if m.get("state")
                .and_then(Value::as_str)
                .is_some_and(|s| matches!(s, "closed" | "merged"))
                || m.get("merged") == Some(&Value::Bool(true)) =>
        {
            false
        }
        Value::Object(m) => ["number", "url", "id"]
            .iter()
            .any(|k| m.get(*k).is_some_and(signal)),
        _ => false,
    }
}
fn class(item: &Task) -> u8 {
    let has = |k: &str| item.fields.get(k).is_some_and(signal);
    let kind = item
        .fields
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("");
    let closed = item
        .fields
        .get("pr_state")
        .and_then(Value::as_str)
        .is_some_and(|s| matches!(s, "closed" | "merged"));
    if !closed
        && ["pr", "pr_url", "pr_number", "pull_request", "open_pr"]
            .iter()
            .any(|k| has(k))
    {
        0
    } else if has("ci_fix")
        || matches!(kind, "ci-fix" | "ci_fix")
        || item
            .key
            .split_once(':')
            .map(|(_, k)| k)
            .unwrap_or(&item.key)
            .to_ascii_uppercase()
            .split('-')
            .collect::<Vec<_>>()
            .windows(2)
            .any(|p| p == ["CI", "FIX"])
    {
        1
    } else {
        2
    }
}
fn reconcile(item: &Task) -> bool {
    let c = item.fields.get("context").unwrap_or(&Value::Null);
    let nonempty = |v: &Value| v.as_array().is_some_and(|a| !a.is_empty());
    nonempty(&c["newer_decisions"])
        || (nonempty(&c["work_exists"]["commits"]) && !nonempty(&c["work_exists"]["lanes"]))
        || ["in_merge_batch", "merge_batch"]
            .iter()
            .any(|k| item.fields.get(*k).is_some_and(signal))
}
/// The keeper's effective priority (own value raised by what the item blocks);
/// the stored priority when the row carries none. The feeder never recomputes it.
fn effective(item: &Task) -> i64 {
    item.fields
        .get("effective_priority")
        .and_then(Value::as_i64)
        .unwrap_or(item.priority)
}
fn age(item: &Task) -> i64 {
    item.created_at
        .as_deref()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|t| t.timestamp_millis())
        .unwrap_or(i64::MAX)
}
/// Eligible items in launch order: PR work, then CI fixes, then the rest; higher priority,
/// then older, first. Optional honest-ready groups override all other data; legacy context
/// guard facts also exclude reconcilable items before the claim activity runs.
fn eligible(ready: &Value, runs: &[Run]) -> Option<Vec<Task>> {
    validate_ready(ready).ok()?;
    let excluded: BTreeSet<&str> = ["needs_reconcile", "in_progress"]
        .iter()
        .filter_map(|k| ready.get(k).and_then(Value::as_array))
        .flatten()
        .filter_map(|v| v.as_str().or_else(|| v["key"].as_str()))
        .collect();
    let mut items: Vec<Task> = ready["items"]
        .as_array()?
        .iter()
        .filter_map(|v| decode_task(v).ok())
        .collect();
    items.retain(|i| {
        !excluded.contains(i.key.as_str())
            && !reconcile(i)
            && i.state.as_deref().is_none_or(|s| s == "open")
            && !runs.iter().any(|r| r.active() && r.holds(&i.key))
            && !runs.iter().any(|r| r.writer() && overlap(i, r))
    });
    items.sort_by(|a, b| {
        class(a)
            .cmp(&class(b))
            .then(effective(b).cmp(&effective(a)))
            .then(age(a).cmp(&age(b)))
            .then(a.key.cmp(&b.key))
    });
    Some(items)
}
pub fn pick(ready: &Value, runs: &[Run], _policy: &Policy) -> Option<Task> {
    eligible(ready, runs)?.into_iter().next()
}
fn related(item: &Task, batch: &[Task]) -> bool {
    batch.iter().any(|member| {
        family(&item.key) == family(&member.key)
            || item.files.iter().any(|a| {
                member
                    .files
                    .iter()
                    .any(|b| crate::paths::paths_overlap(a, b))
            })
    })
}
/// The highest-priority eligible item and the eligible items related to it (same family or
/// sharing files), in launch order, up to `policy.batch_max_items`. Items of a batch share
/// one writer; two batches never share a file because every other item is claimed on its own.
pub fn pick_batch(ready: &Value, runs: &[Run], policy: &Policy) -> Vec<Task> {
    let mut items = eligible(ready, runs).unwrap_or_default().into_iter();
    let Some(seed) = items.next() else {
        return Vec::new();
    };
    let mut batch = vec![seed];
    let mut rest: Vec<Task> = items.collect();
    let order: Vec<String> = batch.iter().chain(&rest).map(|t| t.key.clone()).collect();
    let limit = policy.batch_max_items.max(1);
    // An item related only through a later member still joins: scan until nothing is added.
    while batch.len() < limit {
        let Some(at) = rest.iter().position(|item| related(item, &batch)) else {
            break;
        };
        batch.push(rest.remove(at));
    }
    batch.sort_by_key(|t| order.iter().position(|k| *k == t.key));
    batch
}
