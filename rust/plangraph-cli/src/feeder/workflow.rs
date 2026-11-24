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
    budget: u64,
) -> Result<Vec<(Task, u64)>, SpawnError> {
    let mut claimed: Vec<(Task, u64)> = Vec::new();
    for item in batch {
        // This claim may succeed, so its release must fit too; out of time, give back what is
        // held and let the tick stop (the next tick takes the work).
        if !call_fits(launch, budget, claimed.len() + 1) {
            return Ok(out_of_time(&claimed, item, launch, dropped));
        }
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
        let pending = held.len() + claimed.len() - at;
        if !call_fits(launch, budget, pending) {
            let mut all = held.clone();
            all.extend(claimed[at..].iter().cloned());
            return Ok(out_of_time(&all, item, launch, dropped));
        }
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
/// The tick ran out of time between calls: release every claim still held, record why, and
/// return no batch.
fn out_of_time(
    held: &[(Task, u64)],
    at: &Task,
    launch: &mut impl Launch,
    dropped: &mut Vec<Value>,
) -> Vec<(Task, u64)> {
    dropped.push(json!({"item_key":at.key,"reason":"tick_budget"}));
    for (key, error) in release_held(held, launch) {
        dropped.push(json!({"item_key":key,"reason":format!("release failed: {error}")}));
    }
    Vec::new()
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
fn batch_brief(items: &[Task], rules: &str, reviews: &str) -> String {
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
    let slug = items[0]
        .key
        .rsplit(':')
        .next()
        .unwrap_or("batch")
        .to_ascii_lowercase();
    let input = json!({"items":named, "run":{
        "name":format!("feeder-{slug}"), "worktree":"the run worktree (the current directory)",
        "base":"origin/main at launch", "writer_family":"gpt",
        "status_file":format!("{}/status.txt", reviews.trim_end_matches('/')),
        "fast_checks":["python3 tools/affected_tests.py --base origin/main --head HEAD --local --files-only --run -n 2",
            "python3 tools/preflight.py --base origin/main --head HEAD --lane"],
        "review_cmd":"the native worker starts the review of another family on the exact head",
        "review_wait_seconds":1800, "round_cap":8, "trailers":trailers}});
    format!("PLANGRAPH-BATCH-INPUT-V1\n{input}")
}
/// Time reserved for one claim attempt: claim, reservation check, launch and cleanup each
/// take up to about 20 seconds. A live tick begins an attempt only if this much (or the last
/// attempt's duration, if longer) still fits before its deadline.
const ATTEMPT_RESERVE_SECONDS: u64 = 60;

/// Longest one keeper call takes (curl `--max-time`), and the time reserved to release one
/// held claim after the deadline.
const CALL_SECONDS: u64 = 20;

/// Whether one more keeper call can begin and, with `held` claims then held, still be followed
/// by releasing them all before the budget (counted from service entry) is spent.
fn call_fits(launch: &mut impl Launch, budget: u64, held: usize) -> bool {
    let reserved = CALL_SECONDS.saturating_mul(1 + held as u64);
    launch.elapsed() + std::time::Duration::from_secs(reserved)
        <= std::time::Duration::from_secs(budget)
}

static SERVICE_ENTRY: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();

/// Mark the moment the service entered; the tick deadline counts from here.
pub(super) fn mark_service_entry() {
    SERVICE_ENTRY.get_or_init(std::time::Instant::now);
}

/// Time since `mark_service_entry` (since the first call when it was never marked).
pub(super) fn service_elapsed() -> std::time::Duration {
    SERVICE_ENTRY.get_or_init(std::time::Instant::now).elapsed()
}

/// Whether a live tick `elapsed` after service entry may begin another claim attempt after one
/// that took `last`: it must still fit before the budget, assuming the next takes as long as
/// the last or the fixed reserve, whichever is longer.
fn attempt_fits(
    elapsed: std::time::Duration,
    last: std::time::Duration,
    budget_seconds: u64,
) -> bool {
    elapsed + last.max(std::time::Duration::from_secs(ATTEMPT_RESERVE_SECONDS))
        <= std::time::Duration::from_secs(budget_seconds)
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
    // The tick plays every slot the host has, for a dry run and a live one alike: each batch
    // takes a slot and one run's RAM, and the next pick avoids the files of the batches before
    // it, until admission or the ready list ends the tick. Only the effect of a batch differs:
    // a dry run records it, a live run claims and starts it.
    let mut sim_runs = runs.to_vec();
    let mut sim_facts = facts.clone();
    let mut rows: Vec<Value> = Vec::new();
    let mut stopped = "no_eligible_item".to_string();
    // The refusal row of a tick that already started batches still lists them.
    let failed = |mut row: Value, rows: &[Value]| {
        if !rows.is_empty() {
            row["started"] = json!(rows);
        }
        row
    };
    let mut last_attempt = std::time::Duration::ZERO;
    let mut attempt_started: Option<std::time::Duration> = None;
    loop {
        // The service kills a tick at its timeout, possibly between a claim and its launch:
        // no attempt begins unless claim, reservation, launch and cleanup still fit before the
        // budget counted from service entry (this covers refused attempts and the first one).
        let now = if dry {
            std::time::Duration::ZERO
        } else {
            launch.elapsed()
        };
        if let Some(begun) = attempt_started {
            last_attempt = now.saturating_sub(begun);
        }
        if !dry && !attempt_fits(now, last_attempt, policy.tick_budget_seconds) {
            stopped = format!(
                "tick_budget: {}s of {}s used",
                now.as_secs(),
                policy.tick_budget_seconds
            );
            break;
        }
        attempt_started = Some(now);
        let batch = pick_batch(&ready, &sim_runs, policy);
        let Some(seed) = batch.first() else { break };
        if let Admission::Refuse(reason) = admit(&sim_facts, policy) {
            if rows.is_empty() {
                return json!({"decision":"refused","item_key":seed.key,"reason":reason,"skipped":skipped});
            }
            stopped = reason;
            break;
        }
        let items: Vec<Task> = if dry {
            let mut row = json!({"item_key":seed.key,
                "card":batch_card(&batch, repo, reviews, policy), "brief":batch_brief(&batch, rules, reviews)});
            if batch.len() > 1 {
                row["item_keys"] = json!(batch.iter().map(|t| &t.key).collect::<Vec<_>>());
            }
            rows.push(row);
            batch
        } else {
            let mut dropped = Vec::new();
            let claimed = claim_batch(&batch, launch, &mut dropped, policy.tick_budget_seconds);
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
                    return failed(
                        json!({"decision":"refused","item_key":seed.key,"reason":e.to_string(),"skipped":skipped,"error":true}),
                        &rows,
                    )
                }
            };
            if held.is_empty() {
                continue;
            }
            let items: Vec<Task> = held.iter().map(|(t, _)| t.clone()).collect();
            // The launch gets the time left less the release of every held claim; with less
            // than one call's worth the batch is given back instead.
            let left = std::time::Duration::from_secs(policy.tick_budget_seconds)
                .saturating_sub(launch.elapsed())
                .saturating_sub(std::time::Duration::from_secs(
                    CALL_SECONDS * held.len() as u64,
                ));
            if left < std::time::Duration::from_secs(CALL_SECONDS) {
                let mut gone = Vec::new();
                out_of_time(&held, &held[0].0, launch, &mut gone);
                skipped.extend(gone);
                stopped = "tick_budget: no time left to launch".into();
                break;
            }
            launch.set_launch_limit(left);
            let c = batch_card(&items, repo, reviews, policy);
            match launch.start(&c, &batch_brief(&items, rules, reviews)) {
                Ok(run) => {
                    let mut row = decided(run);
                    row["item_key"] = json!(items[0].key);
                    if items.len() > 1 {
                        row["item_keys"] = json!(items.iter().map(|t| &t.key).collect::<Vec<_>>());
                    }
                    rows.push(row);
                }
                // A launch cut off at its deadline may have been admitted: keep the claims
                // until their lease ends instead of releasing items a run may hold.
                Err(e) if e.starts_with("launch_timeout") => {
                    return failed(
                        json!({"decision":"refused","item_key":items[0].key,"reason":format!("{e}; claims retained"),"skipped":skipped,"error":true}),
                        &rows,
                    );
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
                    return failed(
                        json!({"decision":"refused","item_key":items[0].key,"reason":reason,"skipped":skipped,"error":true}),
                        &rows,
                    );
                }
            }
            items
        };
        sim_runs.push(Run {
            run_id: format!("tick:{}", rows.len()),
            started: None,
            item_key: items[0].key.clone(),
            host: facts.host.clone(),
            status: "running".into(),
            role: "implement".into(),
            files: items.iter().flat_map(|t| t.files.clone()).collect(),
            item_keys: items.iter().map(|t| t.key.clone()).collect(),
        });
        sim_facts.running_writers += 1;
        sim_facts.free_ram_bytes = sim_facts
            .free_ram_bytes
            .saturating_sub(policy.ram_per_run_bytes);
    }
    if let Some(first) = rows.first() {
        let mut row = first.clone();
        if dry {
            row["decision"] = json!("would_spawn");
        }
        row["skipped"] = json!(skipped);
        row["slots"] = json!(rows.len());
        row[if dry { "would_start" } else { "started" }] = json!(rows);
        row["stopped"] = json!(stopped);
        return row;
    }
    if stopped.starts_with("tick_budget") {
        return json!({"decision":"refused","reason":stopped,"skipped":skipped});
    }
    json!({"decision":"idle","reason":"no_eligible_item","skipped":skipped})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn an_attempt_needs_the_reserve_or_the_last_attempt_before_the_deadline() {
        let s = Duration::from_secs;
        // 100 s budget, 60 s reserve: an attempt may begin at 40 s but not at 41 s.
        assert!(attempt_fits(s(40), s(0), 100));
        assert!(!attempt_fits(s(41), s(0), 100));
        // A slow last attempt is assumed to repeat.
        assert!(attempt_fits(s(20), s(80), 100));
        assert!(!attempt_fits(s(21), s(80), 100));
        // 38 s of preflight leaves room for one attempt, not for a second after a 40 s batch.
        assert!(attempt_fits(s(38), s(0), 100));
        assert!(!attempt_fits(s(78), s(40), 100));
    }
}
