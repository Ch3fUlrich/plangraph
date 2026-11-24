//! Pure model of the train actors: registration, selection with bounded deferral, the builder's binding
//! of build, base and policy, CI, retry groups after a red train, conflict resolution, drop and revert,
//! re-admission, settlement (also of a legacy train with no included list) and main moving.
//!
//! `Model::apply` is the transition table; each event is one atomic step of its listed actor. The model
//! owns no clock: `Event::Tick` ages every ready part by one.
//!
//! The objective is batches (parts) merged per hour (rule 3.74 (1), D-6744): by default every ready
//! part rides, a red train drops the failing part and lands the rest. A deferral policy (a size target,
//! a bounded wait on an overlap) is a parameter, not the default.
pub mod replay;
mod retry;
mod rules;
mod select;
mod transition;
mod verify;

use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

pub use rules::{choose, Left, Order, Pool, Rule};

pub type PartId = u8;
pub type TrainId = u8;

/// The merge policy version every build is bound to.
pub const POLICY: u8 = 1;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Unregistered,
    Ready,
    InTrain(TrainId),
    /// In a red train, waiting for its retry group.
    Held,
    Dropped,
    Landed,
}

/// What a part is: the paths it touches (a bit set) and the part it was registered on.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub struct PartSpec {
    pub paths: u16,
    pub parent: Option<PartId>,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Part {
    pub spec: PartSpec,
    pub status: Status,
    pub waited: u8,
    /// A landed part whose outcome was verified: a child may follow it.
    pub verified: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Disposition {
    /// Left out: it overlaps `with`, which rides, and its deferral bound is not used up.
    Overlap { with: PartId, waited: u8 },
    /// Left out: its parent has no verified landing and does not ride in this train.
    ParentNotLanded { parent: PartId },
    /// Left out: the train is at its size target.
    Target,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ci {
    NotStarted,
    Running,
    Green,
    Red,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Ordinary,
    /// A retry group of a red train; `round` counts the red rounds before it.
    Retry {
        lineage: u8,
        round: u8,
    },
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Train {
    pub kind: Kind,
    pub parts: Vec<PartId>,
    /// `None` is a legacy settled train: it recorded no included list.
    pub included: Option<Vec<PartId>>,
    pub left_out: Vec<(PartId, Disposition)>,
    pub base: u8,
    pub bound: bool,
    /// CI has started once: the parts are frozen for good.
    pub sealed: bool,
    pub frozen: Vec<PartId>,
    pub ci: Ci,
    /// Landed, or closed by a red CI.
    pub closed: bool,
    pub landed: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    /// Ordinary trains take at most this many parts.
    pub target: u8,
    /// An overlapping part is deferred while it has waited less than this (ticks); then it rides.
    pub bound: u8,
    /// An ordinary train is due when a part has waited this long or the target is reached.
    pub cut_after: u8,
    pub order: Order,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Group {
    pub lineage: u8,
    pub round: u8,
    pub parts: Vec<PartId>,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub enum Event {
    Register {
        part: PartId,
    },
    Tick,
    Select,
    Join {
        part: PartId,
        train: TrainId,
    },
    Bind {
        train: TrainId,
        build: u8,
        base: u8,
        policy: u8,
    },
    StartCi {
        train: TrainId,
    },
    CiResult {
        train: TrainId,
        green: bool,
    },
    /// The CI log names the failing part: it and its descendants drop, the rest are retried as one group.
    CiRed {
        train: TrainId,
        culprit: PartId,
    },
    Settle {
        train: TrainId,
    },
    /// The settled-train record has no included list (a legacy record).
    SettleLegacy {
        train: TrainId,
    },
    VerifyOutcome {
        train: TrainId,
    },
    Rebuild {
        train: TrainId,
    },
    StartRetry,
    Conflict {
        train: TrainId,
        part: PartId,
    },
    Drop {
        part: PartId,
    },
    Revert {
        part: PartId,
    },
    Readmit {
        part: PartId,
    },
    MainMove,
    /// The registrar records why `part` is not in `train`; the verifier is the check.
    RecordLeftOut {
        train: TrainId,
        part: PartId,
        disposition: Disposition,
    },
    /// The verifier checks one recorded disposition.
    VerifyLeftOut {
        train: TrainId,
        part: PartId,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Outcome {
    Applied,
    Train(TrainId),
    /// A red train made this many retry groups; `escalated` once its lineage reached two red rounds.
    Red {
        groups: usize,
        escalated: bool,
    },
    Dropped,
    Accepted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Denied {
    Phase,
    NotDue,
    RetryFirst,
    OrdinaryOpen,
    CiStarted,
    BindMismatch,
    Unbound,
    Stale,
    NotGreen,
    Disposition,
    Dependency,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    pub(crate) policy: Policy,
    pub(crate) parts: Vec<Part>,
    pub(crate) trains: Vec<Train>,
    pub(crate) groups: Vec<Group>,
    pub(crate) main: u8,
    pub(crate) order: Vec<PartId>,
    pub(crate) lineages: u8,
    pub(crate) escalated: BTreeSet<u8>,
    /// Per lineage: retry runs started and the part count of its red train.
    pub(crate) runs: Vec<(u8, u8, u8)>,
}

impl Policy {
    /// Rule 3.74 (1): every ready part rides; only a missing verified parent holds a part back.
    pub fn every_ready_rides(order: Order) -> Policy {
        Policy {
            target: u8::MAX,
            bound: 0,
            cut_after: 0,
            order,
        }
    }
}

impl Model {
    pub fn new(policy: Policy, specs: impl IntoIterator<Item = PartSpec>) -> Self {
        Model {
            policy,
            parts: specs
                .into_iter()
                .map(|spec| Part {
                    spec,
                    status: Status::Unregistered,
                    waited: 0,
                    verified: false,
                })
                .collect(),
            trains: Vec::new(),
            groups: Vec::new(),
            main: 0,
            order: Vec::new(),
            lineages: 0,
            escalated: BTreeSet::new(),
            runs: Vec::new(),
        }
    }

    pub fn status(&self, part: PartId) -> Status {
        self.parts[part as usize].status
    }
    pub fn main(&self) -> u8 {
        self.main
    }
    pub fn train_parts(&self, train: TrainId) -> &[PartId] {
        &self.trains[train as usize].parts
    }
    pub fn train_ci(&self, train: TrainId) -> Ci {
        self.trains[train as usize].ci
    }
    pub fn left_out(&self, train: TrainId) -> &[(PartId, Disposition)] {
        &self.trains[train as usize].left_out
    }
    pub fn pending_groups(&self) -> Vec<Vec<PartId>> {
        self.groups.iter().map(|g| g.parts.clone()).collect()
    }
    pub fn escalated_lineages(&self) -> usize {
        self.escalated.len()
    }
    /// Retry runs started for `lineage` (not counting rebuilds).
    pub fn retry_runs(&self, lineage: u8) -> u8 {
        self.runs.iter().find(|r| r.0 == lineage).map_or(0, |r| r.1)
    }
    /// Parts landed so far: the numerator of the objective, parts merged per hour.
    pub fn landed(&self) -> usize {
        self.parts
            .iter()
            .filter(|p| p.status == Status::Landed)
            .count()
    }
    pub fn train_count(&self) -> usize {
        self.trains.len()
    }
    pub fn waited(&self, part: PartId) -> u8 {
        self.parts[part as usize].waited
    }
}
