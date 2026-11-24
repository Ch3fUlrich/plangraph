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
/// Claimed members with their epochs, and the items dropped with their reasons.
type Claimed = (Vec<(Task, u64)>, Vec<Value>);
/// Claim every member of `batch` in order, then keep the claims that hold their files against
/// every other claimed item. Only claims this host holds count as its own batch: a member
/// another host holds is a rival like any other item. A refused claim or a yielded reservation
/// drops only that item and releases only its own claim. Infrastructure failures release every
/// claim of this batch.
fn claim_batch(batch: &[Task], launch: &mut impl Launch) -> Result<Claimed, SpawnError> {
    let mut claimed: Vec<(Task, u64)> = Vec::new();
    let mut dropped = Vec::new();
    for item in batch {
        match launch.claim(&item.key) {
            Ok(epoch) => claimed.push((item.clone(), epoch)),
            Err(ClaimError::Refused(reason)) => {
                dropped.push(json!({"item_key":item.key,"reason":reason}));
            }
            Err(e) => {
                release_held(&claimed, launch);
                return Err(SpawnError::Claim(e));
            }
        }
    }
    let own: Vec<String> = claimed.iter().map(|(t, _)| t.key.clone()).collect();
    let mut held: Vec<(Task, u64)> = Vec::new();
    for (item, epoch) in &claimed {
        match launch.rival(&item.key, &item.files, &own) {
            Ok(None) => held.push((item.clone(), *epoch)),
            Ok(Some(rival)) => {
                let reason = match launch.release(&item.key, *epoch) {
                    Ok(()) => format!("files_reserved_by:{rival}"),
                    Err(e) => format!("files_reserved_by:{rival}; release failed: {e}"),
                };
                dropped.push(json!({"item_key":item.key,"reason":reason}));
            }
            Err(e) => {
                release_held(&claimed, launch);
                return Err(SpawnError::Start(format!("reservation check failed: {e}")));
            }
        }
    }
    Ok((held, dropped))
}
fn release_held(held: &[(Task, u64)], launch: &mut impl Launch) {
    for (item, epoch) in held {
        let _ = launch.release(&item.key, *epoch);
    }
}
/// The card and brief of one writer run for `items`: the plain item card for one item, the
/// first item's card with every key and the union of the files for a batch.
fn batch_card(items: &[Task], repo: &str, reviews: &str, policy: &Policy) -> Value {
    let mut c = card(&items[0], repo, reviews, policy);
    if items.len() > 1 {
        let mut files: Vec<&String> = items.iter().flat_map(|t| &t.files).collect();
        files.sort();
        files.dedup();
        c["files"] = json!(files);
        c["item_keys"] = json!(items.iter().map(|t| &t.key).collect::<Vec<_>>());
    }
    c
}
fn batch_brief(items: &[Task], rules: &str) -> String {
    if items.len() == 1 {
        return brief(&items[0], rules);
    }
    let keys: Vec<&str> = items.iter().map(|t| t.key.as_str()).collect();
    let parts: Vec<String> = items.iter().map(|t| brief(t, rules)).collect();
    format!(
        "Batch of {} plan items, implement them in this order: {}\n\n{}",
        items.len(),
        keys.join(", "),
        parts.join("\n----- next item -----\n")
    )
}
/// One bounded tick. Refused claims are decisions about that item, not a reason
/// to starve the rest of the order. Infrastructure/start failures stop the tick.
#[allow(clippy::too_many_arguments)]
pub fn tick(
    ready: &Value,
    runs: &[Run],
    facts: &HostFacts,
    policy: &Policy,
    rules: &str,
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
            let mut row = json!({"decision":"would_spawn",
                "card":batch_card(&batch, repo, reviews, policy), "brief":batch_brief(&batch, rules)});
            row["item_key"] = json!(seed.key);
            row["skipped"] = json!(skipped);
            return row;
        }
        let (held, dropped) = match claim_batch(&batch, launch) {
            Ok(claimed) => claimed,
            Err(e) => {
                return json!({"decision":"refused","item_key":seed.key,"reason":e.to_string(),"skipped":skipped,"error":true})
            }
        };
        for row in &dropped {
            ready["items"]
                .as_array_mut()
                .expect("validated items")
                .retain(|v| v["key"] != row["item_key"]);
        }
        skipped.extend(dropped);
        if held.is_empty() {
            continue;
        }
        let items: Vec<Task> = held.iter().map(|(t, _)| t.clone()).collect();
        let c = batch_card(&items, repo, reviews, policy);
        let started = launch.start(&c, &batch_brief(&items, rules));
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
