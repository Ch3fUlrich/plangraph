//! Pure launch-recovery model. Each successful event is one atomic transition.
//! Inputs named as evidence are observations at the execution host.
mod transition;

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Missing,
    Accepted,
    Starting,
    Running,
    Revoked,
    Published,
    Failed,
    Closed,
}

impl Phase {
    fn open(self) -> bool {
        matches!(
            self,
            Self::Accepted | Self::Starting | Self::Running | Self::Revoked
        )
    }

    fn terminal(self) -> bool {
        matches!(self, Self::Published | Self::Failed | Self::Closed)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub enum Reservation {
    #[default]
    None,
    Reserved,
    Forwarded,
    Mirroring,
    Closed,
}

impl Reservation {
    fn open(self) -> bool {
        matches!(self, Self::Reserved | Self::Forwarded | Self::Mirroring)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub enum Recycle {
    #[default]
    Idle,
    Launching,
    Confirmed,
    StopEligible,
    Stopped,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Boundary {
    Unknown,
    Populated,
    EmptySubtree,
    ManagerVerifiedGone,
}

impl Boundary {
    fn empty(self) -> bool {
        matches!(self, Self::EmptySubtree | Self::ManagerVerifiedGone)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Listing {
    Unreadable,
    Absent,
    Ambiguous,
    Live(u8),
    PresentWithoutStatus(u8),
    Ended(u8),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Event {
    Reserve,
    Forward,
    Receive {
        digest: u8,
        stale: bool,
        name_taken: bool,
    },
    Resolve,
    TakeStart,
    StartChild {
        launcher: u8,
    },
    RefuseBeforeChild {
        launcher: u8,
    },
    OldEnoughToRevoke {
        elapsed_secs: u16,
    },
    NoAttemptProcesses,
    LaunchOutcomeUnknown,
    BindJob(u8),
    Claim(u8),
    NewConversation(u8),
    Delivered {
        job: u8,
        generation: u8,
    },
    StagePublication,
    Publish,
    Listing {
        row: Listing,
        at_secs: u16,
    },
    Boundary(Boundary),
    CleanupJob(u8),
    StopSucceeded(u8),
    PidGone(u8),
    StoreAnswer {
        attempt: usize,
        target: u8,
        phase: Phase,
    },
    MirrorAndClose,
    Acknowledge,
    BeginRecycle,
    Confirm(u8),
    Promote,
    MarkStopEligible,
    StopPredecessor,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Applied,
    Phase(Phase),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Denied {
    Guard,
    Conflict,
    SeatBusy,
    NameTaken,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Spec {
    pub origin: u8,
    pub target: u8,
    pub seat: u8,
    pub name: u8,
    pub digest: u8,
    pub launcher: u8,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Attempt {
    pub spec: Spec,
    pub phase: Phase,
    pub reservation: Reservation,
    pub starts: u8,
    pub bound_job: Option<u8>,
    pub claimed_job: Option<u8>,
    pub recycle: Recycle,
    pub ack: bool,
    pub artifacts: bool,
    stored_answer: Option<Phase>,
    no_processes: bool,
    old_enough: bool,
    generation: u8,
    delivery: Option<u8>,
    publication_proof: Option<u8>,
    boundary: Boundary,
    listing: Listing,
    last_listing_at: Option<u16>,
    absent_since: Option<u16>,
    stopped_job: Option<u8>,
    observed_bound_job: bool,
    stop_verified_by_listing: bool,
    pid_gone_at: Option<u16>,
    revoked_listing_at: Option<u16>,
}

impl Attempt {
    fn new(spec: Spec) -> Self {
        Self {
            spec,
            phase: Phase::Missing,
            reservation: Reservation::None,
            starts: 0,
            bound_job: None,
            claimed_job: None,
            recycle: Recycle::Idle,
            ack: false,
            artifacts: false,
            stored_answer: None,
            no_processes: false,
            old_enough: false,
            generation: 0,
            delivery: None,
            publication_proof: None,
            boundary: Boundary::Unknown,
            listing: Listing::Unreadable,
            last_listing_at: None,
            absent_since: None,
            stopped_job: None,
            observed_bound_job: false,
            stop_verified_by_listing: false,
            pid_gone_at: None,
            revoked_listing_at: None,
        }
    }

    fn live_claim(&self) -> bool {
        self.claimed_job
            .is_some_and(|job| self.listing == Listing::Live(job))
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    attempts: Vec<Attempt>,
    admission_verified: bool,
    claim_window_secs: u16,
    // The execution-host role alias is shared by all attempts for that seat.
    promoted: std::collections::BTreeMap<(u8, u8), usize>,
}

impl Model {
    /// All specs are immutable and indexed by their attempt id.
    pub fn new(
        specs: impl IntoIterator<Item = Spec>,
        admission_verified: bool,
        claim_window_secs: u16,
    ) -> Self {
        Self {
            attempts: specs.into_iter().map(Attempt::new).collect(),
            admission_verified,
            claim_window_secs: claim_window_secs.max(600),
            promoted: Default::default(),
        }
    }

    pub fn attempt(&self, id: usize) -> &Attempt {
        &self.attempts[id]
    }

    /// Check durable invariants after every modeled transition.
    pub fn invariants_hold(&self) -> bool {
        self.attempts.iter().enumerate().all(|(id, row)| {
            let spec = row.spec;
            let other_rows = || {
                self.attempts
                    .iter()
                    .enumerate()
                    .filter(move |(other, _)| *other != id)
                    .map(|(_, row)| row)
            };
            row.starts <= 1
                && (!row.phase.open()
                    || other_rows().all(|other| {
                        !other.phase.open()
                            || (other.spec.target != spec.target || other.spec.seat != spec.seat)
                    }))
                && (!row.reservation.open()
                    || other_rows().all(|other| {
                        !other.reservation.open()
                            || (other.spec.origin != spec.origin || other.spec.seat != spec.seat)
                    }))
                && (row.claimed_job.is_none()
                    || (row.starts == 1 && row.claimed_job == row.bound_job))
                && (row.phase != Phase::Published
                    || (self.admission_verified
                        && row.claimed_job.is_some()
                        && row.artifacts
                        && row.publication_proof.is_some()))
                && (row.reservation != Reservation::Closed
                    || row.stored_answer.is_some_and(Phase::terminal))
                && (!row.ack || (row.reservation == Reservation::Closed && row.phase.terminal()))
                && (row.recycle != Recycle::Stopped || row.phase == Phase::Published)
        })
    }

    /// Check a caller-supplied watchdog bound in beats once the three stop proofs hold.
    pub fn stop_overdue(&self, id: usize, elapsed_beats: u16, bound_beats: u16) -> bool {
        self.attempts.get(id).is_some_and(|row| {
            matches!(row.recycle, Recycle::Confirmed | Recycle::StopEligible)
                && self.stop_proof(id)
                && elapsed_beats > bound_beats
        })
    }

    fn stop_proof(&self, id: usize) -> bool {
        let row = self.attempt(id);
        row.phase == Phase::Published
            && row.live_claim()
            && self.promoted.get(&(row.spec.target, row.spec.seat)) == Some(&id)
    }
}

impl Default for Model {
    fn default() -> Self {
        Self::new(
            [Spec {
                origin: 0,
                target: 1,
                seat: 0,
                name: 0,
                digest: 0,
                launcher: 0,
            }],
            true,
            600,
        )
    }
}
