//! Pure comparison and advancement only. Callers must provide durable CAS separately.
use crate::fleet_owner_lease::{
    Identity, Lease, Phase, Record, Refusal, State, Successor, Transaction,
};

#[derive(Clone, Debug)]
pub enum Transition {
    Acquire(Identity),
    Release,
    Begin(Transaction),
    Launch(String, Successor),
    Confirm(String),
    Finish(String),
    Reconcile {
        evidence_identity: State<Identity>,
        pending: State<Transaction>,
        pause: State<Lease>,
        finalizer: State<Lease>,
        runners: State<Vec<Lease>>,
    },
}

fn known<T>(state: &State<T>) -> bool {
    !matches!(state, State::Unknown)
}
fn resolved(r: &Record) -> bool {
    known(&r.identity)
        && known(&r.pending)
        && known(&r.pause)
        && known(&r.finalizer)
        && known(&r.runners)
}

/// Compare the complete identity (including process start), never process observations.
/// The borrowed input is untouched on success and refusal alike.
pub fn advance(
    record: &Record,
    expected_epoch: u64,
    expected_identity: &State<Identity>,
    transition: Transition,
    now_s: i64,
) -> Result<Record, Refusal> {
    if record.epoch != expected_epoch || &record.identity != expected_identity {
        return Err(Refusal::Invalid);
    }
    record.validate(now_s)?;
    let mut next = record.clone();
    match transition {
        Transition::Reconcile {
            evidence_identity,
            pending,
            pause,
            finalizer,
            runners,
        } => {
            if evidence_identity != record.identity {
                return Err(Refusal::Invalid);
            }
            next.pending = pending;
            next.pause = pause;
            next.finalizer = finalizer;
            next.runners = runners;
            if !resolved(&next) {
                return Err(Refusal::Unresolved);
            }
            next.reconciliation_required = false;
        }
        transition => {
            if !resolved(record)
                || record.reconciliation_required
                || !record.expired_leases(now_s).is_empty()
            {
                return Err(Refusal::Unresolved);
            }
            match (transition, &record.identity, &record.pending) {
                (Transition::Acquire(i), State::Free, _) if record.clear() => {
                    next.identity = State::Busy(i);
                }
                (Transition::Release, State::Busy(_), _) if record.clear() => {
                    next.identity = State::Free;
                }
                (Transition::Begin(t), State::Busy(_), State::Free)
                    if t.phase == Phase::Prepared =>
                {
                    next.pending = State::Busy(t);
                }
                (Transition::Launch(id, successor), State::Busy(_), State::Busy(t))
                    if id == t.id && t.phase == Phase::Prepared =>
                {
                    next.pending = State::Busy(Transaction {
                        phase: Phase::Launched,
                        successor,
                        ..t.clone()
                    });
                }
                (Transition::Confirm(id), State::Busy(_), State::Busy(t))
                    if id == t.id
                        && t.phase == Phase::Launched
                        && matches!(t.successor, Successor::Id(_)) =>
                {
                    next.pending = State::Busy(Transaction {
                        phase: Phase::Confirmed,
                        ..t.clone()
                    });
                }
                (Transition::Finish(id), State::Busy(_), State::Busy(t))
                    if id == t.id && t.phase == Phase::Confirmed =>
                {
                    next.pending = State::Free;
                }
                _ => return Err(Refusal::Unresolved),
            }
        }
    }
    next.epoch = record.epoch.checked_add(1).ok_or(Refusal::Invalid)?;
    next.updated_at = now_s;
    next.validate(now_s)?;
    Ok(next)
}

#[cfg(test)]
#[path = "fleet_owner_transition_tests.rs"]
mod tests;
