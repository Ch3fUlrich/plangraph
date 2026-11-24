//! Pure model of the train actors: registration, selection with bounded deferral, the builder's binding
//! of build, base and policy, CI, retry groups after a red train, conflict resolution, drop and revert,
//! re-admission, settlement (also of a legacy train with no included list), the speculative next train
//! and main moving.
//!
//! `Model::apply` is the transition table; each event is one atomic step of its listed actor. The model
//! owns no clock: `Event::Tick` ages every ready part by one.
//!
//! The objective is batches (parts) merged per hour (rule 3.74 (1), D-6744): by default every ready
//! part rides, a red train drops the failing part and lands the rest. A deferral policy (a size target,
//! a bounded wait on an overlap) is a parameter, not the default.
mod invariants;
mod parts;
pub mod replay;
mod retry;
mod rules;
mod select;
mod spec;
mod transition;
mod types;
mod verify;

pub use rules::{choose, place, Left, Order, Pool, Rule};
pub use types::*;

use std::collections::BTreeSet;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    pub(crate) policy: Policy,
    pub(crate) parts: Vec<Part>,
    pub(crate) trains: Vec<Train>,
    pub(crate) groups: Vec<Group>,
    pub(crate) main: u8,
    pub(crate) order: Vec<PartId>,
    pub(crate) lineages: u8,
    /// The paths each move of main changed: entry `v` is the move from version `v` to `v + 1`.
    pub(crate) moves: Vec<u16>,
    pub(crate) escalated: BTreeSet<u8>,
    /// Per lineage: retry runs started and the part count of its red train.
    pub(crate) runs: Vec<(u8, u8, u8)>,
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
                    deferrals: 0,
                    verified: false,
                    reverted: false,
                })
                .collect(),
            trains: Vec::new(),
            groups: Vec::new(),
            main: 0,
            order: Vec::new(),
            lineages: 0,
            moves: Vec::new(),
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
    pub fn train_closed(&self, train: TrainId) -> bool {
        self.trains[train as usize].closed
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
    /// Red CI rounds this train has had.
    pub fn red_rounds(&self, train: TrainId) -> u8 {
        self.trains[train as usize].red_rounds
    }
    /// The CI run in flight or last started.
    pub fn run(&self, train: TrainId) -> u8 {
        self.trains[train as usize].run
    }
    /// Whether the train's binding of build, base and policy is in place.
    pub fn is_bound(&self, train: TrainId) -> bool {
        self.trains[train as usize].bound
    }
    /// Parts landed so far: the numerator of the objective, parts merged per hour.
    pub fn landed(&self) -> usize {
        self.parts
            .iter()
            .filter(|p| p.status == Status::Landed)
            .count()
    }
    /// Speculative candidates that were promoted.
    pub fn promoted(&self) -> usize {
        self.trains
            .iter()
            .filter(|t| t.landed && matches!(t.kind, Kind::Speculative { .. }))
            .count()
    }
    /// Retry trains landed on a main that had moved since their tested base (the D-6840 experiment).
    pub fn stale_landings(&self) -> usize {
        self.trains.iter().filter(|t| t.stale_landed).count()
    }
    /// Times the train was rebuilt on a moved main.
    pub fn rebuilds(&self, train: TrainId) -> u8 {
        self.trains[train as usize].rebuilds
    }
    pub fn train_count(&self) -> usize {
        self.trains.len()
    }
    pub fn waited(&self, part: PartId) -> u8 {
        self.parts[part as usize].waited
    }
    pub fn deferrals(&self, part: PartId) -> u8 {
        self.parts[part as usize].deferrals
    }
}
