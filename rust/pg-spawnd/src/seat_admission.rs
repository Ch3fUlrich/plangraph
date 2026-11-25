//! Admission of review seats per leg: at most N seats run on one leg (route policy
//! `review_seat_admission`), the rest queue, and a leg that gets limited while they queue is dropped
//! at once because every pass routes again and reads the quota state.
use crate::errors::ToolError;
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path, path::PathBuf, time::Duration};

/// A reservation covers the gap between admission and the run record; past this it is stale.
const LOCK_NAME: &str = "admission.lock";
const RESERVATION_TTL: f64 = 120.0;
/// The run-record field that names the reservation a seat was admitted under.
pub(crate) const TOKEN_FIELD: &str = "seat_reservation";

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
    /// Wall-clock seconds: the time written into slot files shared with other processes.
    fn now(&self) -> f64;
    /// Seconds on a clock that never jumps, for the wait budget.
    fn monotonic(&self) -> f64 {
        self.now()
    }
    fn sleep(&mut self, duration: Duration);
}

/// True for the two refusals that send a review seat to the fallback seats.
pub(crate) fn is_queue_refusal(error: &ToolError) -> bool {
    matches!(error, ToolError::Refused { message, .. }
        if message.starts_with("seat_queue_timeout:") || message.starts_with("seat_leg_limited:"))
}

fn monotonic_clock() -> f64 {
    static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    START
        .get_or_init(std::time::Instant::now)
        .elapsed()
        .as_secs_f64()
}

fn clock() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// Ports for a seat that must stay on the leg it was planned for (one group of a split review): the
/// route never changes, so admission only waits for a slot on that leg.
pub(crate) struct Fixed(pub Value);
impl Ports for Fixed {
    fn route(&mut self) -> Result<Value, ToolError> {
        Ok(self.0.clone())
    }
    fn now(&self) -> f64 {
        clock()
    }
    fn monotonic(&self) -> f64 {
        monotonic_clock()
    }
    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
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
        clock()
    }
    fn monotonic(&self) -> f64 {
        monotonic_clock()
    }
    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

/// Holds one seat slot until the seat's run record takes over; dropping it frees the slot.
pub(crate) struct Reservation(PathBuf);
impl Reservation {
    /// The name the seat's run record carries (`TOKEN_FIELD`), so the record and the slot are one seat.
    pub(crate) fn token(&self) -> String {
        self.0
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    }
}
impl Reservation {
    /// Leaves the slot in place for the run record that names its token; occupancy finishes it once
    /// that record exists (or after the reservation's time to live).
    pub(crate) fn keep(self) {
        std::mem::forget(self);
    }
}
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

/// The leg a review run occupies: the leg that served it, else the leg of its running attempt (a
/// quota retry moves a run to a later leg of its plan), else the plan's first leg.
fn leg_of(record: &Value) -> Option<&str> {
    record["chosen_leg"]["id"]
        .as_str()
        .or_else(|| {
            record["attempts"]
                .as_array()?
                .iter()
                .rev()
                .find(|attempt| attempt["status"] == "running")?["leg"]["id"]
                .as_str()
        })
        .or_else(|| record["plan"]["first_leg"]["id"].as_str())
}

/// Seats on `leg`: live review runs, plus reservations no run record names yet. A reservation whose
/// token a run record carries is that seat's: the run counts itself and the reservation is finished.
fn occupied(state: &Path, records: &[Value], leg: &str, now: f64) -> (usize, Vec<PathBuf>) {
    let live = records
        .iter()
        .filter(|r| {
            r["role"] == "review"
                && !crate::runs::terminal(r)
                && r["dry_run"] != true
                && leg_of(r) == Some(leg)
        })
        .count();
    let named: std::collections::BTreeSet<&str> = records
        .iter()
        .filter_map(|r| r[TOKEN_FIELD].as_str())
        .collect();
    let dir = slot_dir(state, leg);
    let mut count = live;
    let mut finished = Vec::new();
    for entry in std::fs::read_dir(&dir).into_iter().flatten().flatten() {
        let path = entry.path();
        let Some(token) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if token == LOCK_NAME {
            continue;
        }
        let Some(at) = std::fs::read_to_string(&path)
            .ok()
            .and_then(|t| t.trim().parse::<f64>().ok())
        else {
            continue;
        };
        if named.contains(token) || now - at >= RESERVATION_TTL {
            finished.push(path);
        } else {
            count += 1;
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
    let path = dir.join(LOCK_NAME);
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

/// A slot on `leg` now or a refusal, never a wait: for a run that moves to a later leg on its own.
/// `policy` is the `seat_admission` document of the run's route plan.
pub(crate) fn reserve_now(
    state: &Path,
    leg: &Value,
    policy: &Value,
) -> Result<Option<Reservation>, ToolError> {
    let mut plan = json!({"first_leg": leg, "legs": [leg], "seat_admission": policy});
    if let Some(doc) = plan["seat_admission"].as_object_mut() {
        doc.insert("queue_wait_seconds".into(), json!(0));
    }
    Ok(admit(state, plan.clone(), &mut Fixed(plan))?.1)
}

/// Takes a slot for the plan's first leg. A full leg queues the seat: it waits and routes again each
/// poll (a limited leg drops out of the route at once). The seat never falls through to another leg
/// of the queued leg's harness: past the wait bound it is refused `seat_queue_timeout`, and a route
/// that drops the leg for a sibling of the same harness is refused `seat_leg_limited`, so the caller
/// applies its fallback seats. A route that moves to another harness is that fallback and is taken.
pub(crate) fn admit<P: Ports>(
    state: &Path,
    mut plan: Value,
    ports: &mut P,
) -> Result<(Value, Option<Reservation>), ToolError> {
    let began = ports.monotonic();
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
        let lock = lock_leg(state, &id)?;
        let (count, stale) = occupied(state, &crate::records::list_runs(state), &id, ports.now());
        stale.iter().for_each(|p| {
            let _ = std::fs::remove_file(p);
        });
        // Read after the lock and the occupancy scan, which can take long: the budget is checked
        // against the time the slot would actually be taken.
        let waited = ports.monotonic() - began;
        // A seat that queued past its budget goes to the fallback even when a slot just freed.
        let expired = queued.is_some() && waited >= policy.wait.as_secs_f64();
        if count < limit && !expired {
            let path = slot_dir(state, &id).join(format!("{}-{}", std::process::id(), nonce()));
            std::fs::write(&path, format!("{}\n", ports.now()))
                .map_err(|e| ToolError::Internal(e.to_string()))?;
            return Ok((plan, Some(Reservation(path))));
        }
        drop(lock);
        if waited >= policy.wait.as_secs_f64() {
            return Err(queue_timeout(&id, count, limit, waited));
        }
        queued = Some((id.clone(), leg["harness"].clone()));
        // Never sleep past the budget: the last poll is the time left.
        let left = Duration::from_secs_f64((policy.wait.as_secs_f64() - waited).max(0.0));
        ports.sleep(policy.poll.min(left));
        // The budget is checked before the route is asked again and after it answers: a seat whose
        // last sleep or whose route call spent the budget is refused, never admitted on what it found.
        let spent = |ports: &mut P| ports.monotonic() - began >= policy.wait.as_secs_f64();
        if spent(ports) {
            return Err(queue_timeout(&id, count, limit, ports.monotonic() - began));
        }
        plan = match ports.route() {
            Ok(plan) => plan,
            // The `agy-cli` selector found no agy leg left: the queued leg was limited and nothing of
            // its harness is routable, which is the same-harness case below, not a bad request.
            Err(ToolError::Refused {
                kind: crate::errors::RefusalKind::Parameter,
                message,
                ..
            }) if message == crate::cli_review::NO_LEG => {
                return Err(refused(format!(
                    "seat_leg_limited: {id} left the route while the seat queued and no leg of its harness is routable; use the fallback seats"
                )));
            }
            Err(error) => return Err(error),
        };
        if spent(ports) {
            return Err(queue_timeout(&id, count, limit, ports.monotonic() - began));
        }
    }
}

fn queue_timeout(id: &str, count: usize, limit: usize, waited: f64) -> ToolError {
    refused(format!(
        "seat_queue_timeout: {id} runs {count} of {limit} seats after {waited:.0}s; use the fallback seats"
    ))
}

/// Unique within the process: two seats admitted in the same instant never share a slot file.
fn nonce() -> u64 {
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
}

#[cfg(test)]
#[path = "seat_admission_tests.rs"]
mod tests;
