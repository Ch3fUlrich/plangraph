//! Admission of review seats per leg: at most N seats run on one leg (route policy
//! `review_seat_admission`), the rest queue, and a leg that gets limited while they queue is dropped
//! at once because every pass routes again and reads the quota state.
use crate::errors::ToolError;
use serde_json::Value;
use std::{collections::BTreeMap, path::Path, path::PathBuf, time::Duration};

/// A reservation covers the gap between admission and the run record; past this it is stale.
const RESERVATION_TTL: f64 = 120.0;
/// A record this much older than the reservation still belongs to it (clock rounding).
const MATCH_SLACK: f64 = 1.0;

pub(crate) struct Policy {
    limits: BTreeMap<String, usize>,
    wait: Duration,
    poll: Duration,
}

impl Policy {
    /// The policy a route plan carries; `None` when it bounds no leg.
    pub(crate) fn from_plan(plan: &Value) -> Option<Self> {
        let doc = plan.get("seat_admission")?;
        let limits: BTreeMap<_, _> = doc["legs"]
            .as_object()?
            .iter()
            .filter_map(|(leg, n)| Some((leg.clone(), usize::try_from(n.as_u64()?).ok()?.max(1))))
            .collect();
        let seconds = |key: &str, default: f64| {
            Duration::from_secs_f64(doc[key].as_f64().filter(|s| *s >= 0.0).unwrap_or(default))
        };
        (!limits.is_empty()).then(|| Self {
            limits,
            wait: seconds("queue_wait_seconds", 900.0),
            poll: seconds("poll_seconds", 15.0).max(Duration::from_millis(10)),
        })
    }
}

/// What admission needs from the outside, so the queue can be tested without clocks or a router.
pub(crate) trait Ports {
    fn route(&mut self) -> Result<Value, ToolError>;
    fn now(&self) -> f64;
    fn sleep(&mut self, duration: Duration);
}

/// The production ports: the real clock and the route adapter.
pub(crate) struct Live<'a> {
    pub state: &'a Path,
    pub args: &'a crate::placement::Args,
}
impl Ports for Live<'_> {
    fn route(&mut self) -> Result<Value, ToolError> {
        serde_json::from_str(&crate::placement::route(self.state, self.args)?)
            .map_err(|_| ToolError::Internal("route adapter returned invalid output".into()))
    }
    fn now(&self) -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    }
    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

/// Holds one seat slot until the seat's run record takes over; dropping it frees the slot.
pub(crate) struct Reservation(PathBuf);
impl Drop for Reservation {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn refused(message: String) -> ToolError {
    ToolError::Refused {
        kind: crate::errors::RefusalKind::Spawn,
        message,
        legs: vec![],
    }
}

fn leg_of(record: &Value) -> Option<&str> {
    record["chosen_leg"]["id"]
        .as_str()
        .or_else(|| record["plan"]["first_leg"]["id"].as_str())
}

/// Seats on `leg`: live review runs, plus reservations no run record has taken over yet.
fn occupied(state: &Path, records: &[Value], leg: &str, now: f64) -> (usize, Vec<PathBuf>) {
    let mut live: Vec<f64> = records
        .iter()
        .filter(|r| {
            r["role"] == "review"
                && !crate::runs::terminal(r)
                && r["dry_run"] != true
                && leg_of(r) == Some(leg)
        })
        .filter_map(|r| r["started"].as_f64())
        .collect();
    live.sort_by(f64::total_cmp);
    let dir = slot_dir(state, leg);
    let mut held: Vec<(f64, PathBuf)> = std::fs::read_dir(&dir)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|e| {
            let at = std::fs::read_to_string(e.path())
                .ok()?
                .trim()
                .parse()
                .ok()?;
            Some((at, e.path()))
        })
        .collect();
    held.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut count = live.len();
    let mut taken = vec![false; live.len()];
    let mut finished = Vec::new();
    for (at, path) in held {
        let matched = live
            .iter()
            .enumerate()
            .find(|(i, started)| !taken[*i] && **started >= at - MATCH_SLACK);
        if let Some((i, _)) = matched {
            taken[i] = true;
            finished.push(path);
        } else if now - at < RESERVATION_TTL {
            count += 1;
        } else {
            finished.push(path);
        }
    }
    (count, finished)
}

fn slot_dir(state: &Path, leg: &str) -> PathBuf {
    let safe: String = leg
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect();
    state.join("seat-slots").join(safe)
}

fn lock_leg(state: &Path, leg: &str) -> Result<pg_worker_core::lockfile::LockGuard, ToolError> {
    let dir = slot_dir(state, leg);
    std::fs::create_dir_all(&dir).map_err(|e| ToolError::Internal(e.to_string()))?;
    let path = dir.join("admission.lock");
    for _ in 0..500 {
        match pg_worker_core::lockfile::try_lock(&path) {
            Ok(Some(guard)) => return Ok(guard),
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(e) => return Err(ToolError::Internal(e.to_string())),
        }
    }
    Err(refused(
        "seat_admission_busy: leg admission lock is held".into(),
    ))
}

/// Takes a slot for the plan's first leg. A full leg queues the seat: it waits and routes again each
/// poll (a limited leg drops out of the route at once). The seat never falls through to another leg
/// of the queued leg's harness: past the wait bound it is refused `seat_queue_timeout`, and a route
/// that drops the leg for a sibling of the same harness is refused `seat_leg_limited`, so the caller
/// applies its fallback seats. A route that moves to another harness is that fallback and is taken.
pub(crate) fn admit(
    state: &Path,
    mut plan: Value,
    ports: &mut impl Ports,
) -> Result<(Value, Option<Reservation>), ToolError> {
    let began = ports.now();
    let mut queued: Option<(String, Value)> = None;
    loop {
        let leg = plan["first_leg"].clone();
        let id = leg["id"].as_str().unwrap_or_default().to_owned();
        if let Some((was, harness)) = &queued {
            if *was != id && leg["harness"] == *harness {
                return Err(refused(format!(
                    "seat_leg_limited: {was} left the route while the seat queued and {id} is the same harness; use the fallback seats"
                )));
            }
        }
        let Some(policy) = Policy::from_plan(&plan) else {
            return Ok((plan, None));
        };
        let Some(limit) = policy.limits.get(&id).copied() else {
            return Ok((plan, None));
        };
        let waited = ports.now() - began;
        let lock = lock_leg(state, &id)?;
        let (count, stale) = occupied(state, &crate::records::list_runs(state), &id, ports.now());
        stale.iter().for_each(|p| {
            let _ = std::fs::remove_file(p);
        });
        if count < limit {
            let path = slot_dir(state, &id).join(format!("{}-{}", std::process::id(), nonce()));
            std::fs::write(&path, format!("{}\n", ports.now()))
                .map_err(|e| ToolError::Internal(e.to_string()))?;
            return Ok((plan, Some(Reservation(path))));
        }
        drop(lock);
        if waited >= policy.wait.as_secs_f64() {
            return Err(refused(format!(
                "seat_queue_timeout: {id} runs {count} of {limit} seats after {waited:.0}s; use the fallback seats"
            )));
        }
        queued = Some((id, leg["harness"].clone()));
        ports.sleep(policy.poll);
        plan = ports.route()?;
    }
}

/// Unique within the process: two seats admitted in the same instant never share a slot file.
fn nonce() -> u64 {
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
}

#[cfg(test)]
#[path = "seat_admission_tests.rs"]
mod tests;
