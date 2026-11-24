use super::*;
use std::fmt;
#[derive(Debug)]
pub enum ClaimError {
    Refused(String),
    Unavailable(String),
}
impl fmt::Display for ClaimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(e) => write!(f, "claim refused: {e}"),
            Self::Unavailable(e) => write!(f, "claim unavailable: {e}"),
        }
    }
}
#[derive(Debug)]
pub enum SpawnError {
    Claim(ClaimError),
    Start(String),
}
impl fmt::Display for SpawnError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Claim(e) => e.fmt(f),
            Self::Start(e) => e.fmt(f),
        }
    }
}
/// Claim every member of `batch` in order, then keep the claims that hold their files against
/// every other claimed item. Only claims this host holds count as its own batch: a member
/// another host holds is a rival like any other item. A refused claim or a yielded reservation
/// drops only that item and releases only its own claim. Infrastructure failures release every
/// claim of this batch. The items dropped on the way, with their reasons, go to `dropped` even
/// when the batch fails.
fn claim_batch(
    batch: &[Task],
    launch: &mut impl Launch,
    dropped: &mut Vec<Value>,
) -> Result<Vec<(Task, u64)>, SpawnError> {
    let mut claimed: Vec<(Task, u64)> = Vec::new();
    for item in batch {
        match launch.claim(&item.key) {
            Ok(epoch) => claimed.push((item.clone(), epoch)),
            Err(ClaimError::Refused(reason)) => {
                dropped.push(json!({"item_key":item.key,"reason":reason}));
                // The highest-priority item is out: the caller picks again from the rest.
                if claimed.is_empty() && item.key == batch[0].key {
                    return Ok(Vec::new());
                }
            }
            Err(e) => {
                let failures = release_held(&claimed, launch);
                return Err(SpawnError::Start(with_failures(e.to_string(), &failures)));
            }
        }
    }
    // Claims this host still holds or has not checked yet: a member that yielded is released
    // and no longer a batch mate for the checks after it.
    let mut own: Vec<String> = claimed.iter().map(|(t, _)| t.key.clone()).collect();
    let mut held: Vec<(Task, u64)> = Vec::new();
    for (at, (item, epoch)) in claimed.iter().enumerate() {
        match launch.rival(&item.key, &item.files, &own) {
            Ok(None) => held.push((item.clone(), *epoch)),
            Ok(Some(rival)) => {
                let reason = match launch.release(&item.key, *epoch) {
                    Ok(()) => format!("files_reserved_by:{rival}"),
                    Err(e) => format!("files_reserved_by:{rival}; release failed: {e}"),
                };
                own.retain(|key| *key != item.key);
                dropped.push(json!({"item_key":item.key,"reason":reason}));
            }
            Err(e) => {
                // Release what is still claimed: the held members and this one with the
                // unchecked rest. A yielded member was released already.
                let mut pending = held.clone();
                pending.extend(claimed[at..].iter().cloned());
                let failures = release_held(&pending, launch);
                return Err(SpawnError::Start(with_failures(
                    format!("reservation check failed: {e}"),
                    &failures,
                )));
            }
        }
    }
    // A batch without its seed is not the next work in priority order: release it whole and
    // let the caller pick again, so lower-priority items never overtake the seed's slot.
    if held.first().is_none_or(|(t, _)| t.key != batch[0].key) {
        // A claim that cannot be released stays held until its lease ends: say so.
        for (key, error) in release_held(&held, launch) {
            dropped.push(json!({"item_key":key,"reason":format!("release failed: {error}")}));
        }
        return Ok(Vec::new());
    }
    Ok(held)
}
/// Release each claim by its own epoch; the failures, as (key, error), stay held until expiry.
fn release_held(held: &[(Task, u64)], launch: &mut impl Launch) -> Vec<(String, String)> {
    held.iter()
        .filter_map(|(item, epoch)| {
            launch
                .release(&item.key, *epoch)
                .err()
                .map(|e| (item.key.clone(), e))
        })
        .collect()
}
fn with_failures(reason: String, failures: &[(String, String)]) -> String {
    failures.iter().fold(reason, |reason, (key, error)| {
        format!("{reason}; release of {key} failed: {error}")
    })
}
/// The card and brief of one writer run for `items`: the plain item card for one item, the
/// first item's card with every key and the union of the files for a batch.
fn batch_card(items: &[Task], repo: &str, reviews: &str, policy: &Policy) -> Value {
    let mut c = card(&items[0], repo, reviews, policy);
    c["writer_policy"] = json!(policy.writer_policy);
    if items.len() > 1 {
        let mut files: Vec<&String> = items.iter().flat_map(|t| &t.files).collect();
        files.sort();
        files.dedup();
        c["files"] = json!(files);
        c["item_keys"] = json!(items.iter().map(|t| &t.key).collect::<Vec<_>>());
    }
    c
}
/// The commit gate requires the identity of the native feeder writer.
const WRITER_TRAILER: &str = "Writer: codex/gpt-6.2-sol-medium (native-codex spawner run)";
fn batch_brief(items: &[Task], policy: &Policy, reviews: &str) -> Result<String, String> {
    use pg_worker_core::writer_brief::{Input, Item, Review, RunValues};
    let named = items
        .iter()
        .map(|task| {
            let mut context = Vec::new();
            if let Some(note) = task
                .fields
                .get("note")
                .and_then(Value::as_str)
                .filter(|note| !note.trim().is_empty())
            {
                context.push(format!("Notes:\n{note}"));
            }
            let metadata = task_metadata(task);
            if !metadata.is_empty() {
                context.push(format!("Task metadata:\n{}", json!(metadata)));
            }
            Item {
                key: task.key.clone(),
                title: task.title.clone(),
                description: task.description.clone().unwrap_or_default(),
                done_criteria: task.done_criteria.as_str().unwrap_or_default().into(),
                files: task.files.clone(),
                context: (!context.is_empty()).then(|| context.join("\n\n")),
            }
        })
        .collect();
    let mut trailers: Vec<String> = items
        .iter()
        .map(|t| format!("Plan-Item: {}", t.key))
        .collect();
    trailers.push(WRITER_TRAILER.into());
    trailers.push("Harness: codex".into());
    trailers.push("Changelog: <fragment path or skip (reason)>".into());
    let slug = items[0]
        .key
        .rsplit(':')
        .next()
        .unwrap_or("batch")
        .to_ascii_lowercase();
    Input {
        items: named,
        run: RunValues {
            name: format!("feeder-{slug}"),
            base: "origin/main".into(),
            status_file: format!("{}/status.txt", reviews.trim_end_matches('/')),
            review: Review::Driver {},
            policy_revision: policy.writer_policy.revision.clone(),
            rules: policy.writer_policy.rules.clone(),
            trailers,
        },
        policy: policy.writer_policy.clone(),
        context: None,
        pushed_fix: None,
    }
    .encode()
}
/// One bounded tick. Refused claims are decisions about that item, not a reason
/// to starve the rest of the order. Infrastructure/start failures stop the tick.
#[allow(clippy::too_many_arguments)]
pub fn tick(
    ready: &Value,
    runs: &[Run],
    facts: &HostFacts,
    policy: &Policy,
    _legacy_rules: &str,
    repo: &str,
    reviews: &str,
    dry: bool,
    launch: &mut impl Launch,
) -> Value {
    let (mut ready, mut skipped) = match prepare_ready(ready) {
        Ok(v) => v,
        Err(e) => return json!({"decision":"refused","reason":e,"error":true}),
    };
    loop {
        let batch = pick_batch(&ready, runs, policy);
        let Some(seed) = batch.first() else { break };
        if let Admission::Refuse(reason) = admit(facts, policy) {
            return json!({"decision":"refused","item_key":seed.key,"reason":reason,"skipped":skipped});
        }
        if dry {
            let brief = match batch_brief(&batch, policy, reviews) {
                Ok(brief) => brief,
                Err(reason) => {
                    return json!({"decision":"refused","item_key":seed.key,"reason":reason,"skipped":skipped,"error":true})
                }
            };
            let mut row = json!({"decision":"would_spawn",
                "card":batch_card(&batch, repo, reviews, policy), "brief":brief});
            row["item_key"] = json!(seed.key);
            row["skipped"] = json!(skipped);
            return row;
        }
        let mut dropped = Vec::new();
        let claimed = claim_batch(&batch, launch, &mut dropped);
        for row in &dropped {
            ready["items"]
                .as_array_mut()
                .expect("validated items")
                .retain(|v| v["key"] != row["item_key"]);
        }
        skipped.extend(dropped);
        let held = match claimed {
            Ok(held) => held,
            Err(e) => {
                return json!({"decision":"refused","item_key":seed.key,"reason":e.to_string(),"skipped":skipped,"error":true})
            }
        };
        if held.is_empty() {
            continue;
        }
        let items: Vec<Task> = held.iter().map(|(t, _)| t.clone()).collect();
        let c = batch_card(&items, repo, reviews, policy);
        let started =
            batch_brief(&items, policy, reviews).and_then(|brief| launch.start(&c, &brief));
        match started {
            Ok(run) => {
                let mut row = decided(run);
                row["item_key"] = json!(items[0].key);
                if items.len() > 1 {
                    row["item_keys"] = json!(items.iter().map(|t| &t.key).collect::<Vec<_>>());
                }
                row["skipped"] = json!(skipped);
                return row;
            }
            Err(e) => {
                let failures: Vec<String> = held
                    .iter()
                    .filter_map(|(t, epoch)| launch.release(&t.key, *epoch).err())
                    .collect();
                let reason = match failures.first() {
                    None => format!("spawn failed; claim released: {e}"),
                    Some(release) => format!("spawn failed: {e}; release failed: {release}"),
                };
                return json!({"decision":"refused","item_key":items[0].key,"reason":reason,"skipped":skipped,"error":true});
            }
        }
    }
    json!({"decision":"idle","reason":"no_eligible_item","skipped":skipped})
}
