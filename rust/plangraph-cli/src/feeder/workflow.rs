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
/// The status file of one launch, directly inside the review directory: the file-safe slug of
/// the first key, a digest of every raw key (so `FOO.BAR-1` and `FOO_BAR-1` differ) and the
/// first claim's epoch (so a retry of the same items differs). The card carries it as
/// `writer_status_path` (the native writer host records it) and the brief quotes the same path
/// as STATUS_FILE, so a run never reads or writes another run's DONE lines. A dry run has no
/// claim and uses epoch 0.
fn status_path(items: &[Task], epoch: u64, reviews: &str) -> String {
    use sha2::{Digest, Sha256};
    let slug: String = items[0]
        .key
        .rsplit(':')
        .next()
        .unwrap_or("batch")
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect();
    let mut digest = Sha256::new();
    for item in items {
        digest.update(item.key.as_bytes());
        digest.update([0]);
    }
    let digest: String = digest.finalize()[..4]
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let slug = if slug.is_empty() { "batch" } else { &slug };
    format!(
        "{}/status-{slug}-{digest}-e{epoch}.txt",
        reviews.trim_end_matches('/')
    )
}
/// The card and brief of one writer run for `items`: the plain item card for one item, the
/// first item's card with every key and the union of the files for a batch.
/// The run name, and with it the lane, of a feeder batch: `feeder-<first item slug>`.
fn run_name(first_key: &str) -> String {
    let slug = first_key
        .rsplit(':')
        .next()
        .unwrap_or("batch")
        .to_ascii_lowercase();
    format!("feeder-{slug}")
}

fn batch_card(items: &[Task], repo: &str, reviews: &str, status: &str, policy: &Policy) -> Value {
    let mut c = card(&items[0], repo, reviews, policy);
    c["writer_status_path"] = json!(status);
    // The native host pushes the accepted head with its receipt to this lane (nothing is pushed
    // without a target); the brief's run name and this ref name the same lane.
    c["push_ref"] = json!(format!("refs/heads/lane/{}", run_name(&items[0].key)));
    if items.len() > 1 {
        let mut files: Vec<&String> = items.iter().flat_map(|t| &t.files).collect();
        files.sort();
        files.dedup();
        c["files"] = json!(files);
        c["item_keys"] = json!(items.iter().map(|t| &t.key).collect::<Vec<_>>());
    }
    c
}
/// The native-codex identity every commit of a feeder run carries (deploy/feeder/house-rules.md);
/// the commit gate refuses a commit without a Writer trailer.
const WRITER_TRAILER: &str = "Writer: codex/gpt-6.2-sol-medium (native-codex spawner run)";
/// The shared batch writer input (`PLANGRAPH-BATCH-INPUT-V1`) that pg-spawnd renders into the
/// `# Items` and `# Run values` sections, so a batch gets the batch-writer skill. The native
/// worker enforces its own fast-check and review policy; the values here are the brief's text.
fn batch_brief(items: &[Task], rules: &str, status: &str) -> String {
    if items.len() == 1 {
        return brief(&items[0], rules);
    }
    let named: Vec<Value> = items
        .iter()
        .map(|t| {
            let title = t.title.lines().next().unwrap_or("").trim();
            json!({"key":t.key, "title":if title.is_empty() { &t.key } else { title },
                "brief":brief(t, rules)})
        })
        .collect();
    let mut trailers: Vec<String> = items
        .iter()
        .map(|t| format!("Plan-Item: {}", t.key))
        .collect();
    trailers.push(WRITER_TRAILER.into());
    trailers.push("Changelog: a changelog.d/<ITEM>.md fragment, or a Changelog: skip (<reason>) trailer on every commit without one".into());
    let input = json!({"items":named, "run":{
        "name":run_name(&items[0].key), "worktree":"the run worktree (the current directory)",
        "base":"origin/main at launch", "writer_family":"gpt",
        "status_file":status,
        // The native host pushes the accepted head and its receipt itself: no writer push line.
        "push":false,
        "review_cmd":"the native worker starts the review of another family on the exact head",
        "review_wait_seconds":1800, "round_cap":8, "trailers":trailers}});
    format!("PLANGRAPH-BATCH-INPUT-V1\n{input}")
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
    let mut sim_runs = runs.to_vec();
    let mut sim_facts = facts.clone();
    let mut rows = Vec::new();
    let stop = loop {
        let batch = pick_batch(&ready, &sim_runs, policy);
        let Some(seed) = batch.first() else {
            break json!({"decision":"idle","reason":"no_eligible_item"});
        };
        if let Admission::Refuse(reason) = admit(&sim_facts, policy) {
            break json!({"decision":"refused","item_key":seed.key,"reason":reason});
        }
        let (items, mut row) = if dry {
            let status = status_path(&batch, 0, reviews);
            let row = json!({"decision":"would_spawn",
                "card":batch_card(&batch, repo, reviews, &status, policy),
                "brief":batch_brief(&batch, rules, &status)});
            (batch, row)
        } else {
            let mut dropped = Vec::new();
            let claimed = claim_batch(&batch, launch, &mut dropped);
            ready["items"]
                .as_array_mut()
                .expect("validated items")
                .retain(|item| !dropped.iter().any(|drop| item["key"] == drop["item_key"]));
            skipped.extend(dropped);
            let held = match claimed {
                Ok(held) => held,
                Err(e) => {
                    break json!({"decision":"refused","item_key":seed.key,"reason":e.to_string(),"error":true});
                }
            };
            if held.is_empty() {
                continue;
            }
            let items: Vec<Task> = held.iter().map(|(task, _)| task.clone()).collect();
            let status = match launch.allocate_status(&status_path(&items, held[0].1, reviews)) {
                Ok(status) => status,
                Err(e) => {
                    let failures = release_held(&held, launch);
                    let reason = with_failures(format!("status file unavailable: {e}"), &failures);
                    break json!({"decision":"refused","item_key":items[0].key,"reason":reason,"error":true});
                }
            };
            let card = batch_card(&items, repo, reviews, &status, policy);
            match launch.start(&card, &batch_brief(&items, rules, &status)) {
                Ok(run) => (items, decided(run)),
                Err(e) => {
                    let failures = release_held(&held, launch);
                    let reason = if failures.is_empty() {
                        format!("spawn failed; claim released: {e}")
                    } else {
                        failures.iter().fold(
                            format!("spawn failed: {e}"),
                            |reason, (key, error)| {
                                format!("{reason}; release failed: {error} ({key})")
                            },
                        )
                    };
                    break json!({"decision":"refused","item_key":items[0].key,"reason":reason,"error":true});
                }
            }
        };
        let keys: Vec<String> = items.iter().map(|item| item.key.clone()).collect();
        row["item_key"] = json!(keys[0]);
        if keys.len() > 1 {
            row["item_keys"] = json!(keys);
        }
        rows.push(row);
        // Every accepted or simulated run consumes the same RAM and holds its scope until
        // the next tick resamples the host. A queued launch still owns its claims.
        sim_facts.running_writers += 1;
        sim_facts.free_ram_bytes = sim_facts
            .free_ram_bytes
            .saturating_sub(policy.ram_per_run_bytes);
        sim_runs.push(Run {
            item_key: keys[0].clone(),
            item_keys: keys,
            files: items.into_iter().flat_map(|item| item.files).collect(),
            status: "running".into(),
            role: "implement".into(),
            ..Run::default()
        });
    };
    let mut result = if let Some(first) = rows.first() {
        let mut result = first.clone();
        result["slots"] = json!(rows.len());
        result["stopped"] = stop["reason"].clone();
        result[if dry { "would_start" } else { "started" }] = json!(rows);
        if stop["error"] == true {
            skipped.push(json!({"item_key":stop["item_key"],"reason":stop["reason"]}));
        }
        result
    } else {
        stop
    };
    result["skipped"] = json!(skipped);
    result
}
