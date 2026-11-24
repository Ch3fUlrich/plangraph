//! The data of the model: parts, trains, policy, events and their answers.
use super::rules::Order;
use serde::{Deserialize, Serialize};

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
    /// Times left out as Overlap or Target.
    pub deferrals: u8,
    /// A landed part whose outcome was verified: a child may follow it.
    pub verified: bool,
    /// Landed once and reverted since: a child registered on it is refused.
    pub reverted: bool,
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
    /// Left out: the train is at the keeper's limit of parts.
    Cap,
    /// Left out: its merge conflicts and no resolution was admitted.
    Conflict,
    /// Left out: it was dropped.
    Dropped,
    /// Left out: `of`, a part below it in its stack, is not in this train.
    Descendant { of: PartId },
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
    /// The next candidate, built on the unlanded train `pred` and tested meanwhile.
    Speculative {
        pred: TrainId,
    },
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Train {
    pub kind: Kind,
    pub parts: Vec<PartId>,
    /// `None` is a legacy settled train: it recorded no included list.
    pub included: Option<Vec<PartId>>,
    pub left_out: Vec<(PartId, Disposition)>,
    /// Every part that was ready when the builder cut the train.
    pub ready_at_cut: Vec<PartId>,
    /// Parts whose merge conflicted at build.
    pub conflicted: Vec<PartId>,
    /// The main version this train was built on (for a speculative one, the one after `pred` lands).
    pub base: u8,
    pub bound: bool,
    /// CI has started once: the parts are frozen for good.
    pub sealed: bool,
    pub frozen: Vec<PartId>,
    pub ci: Ci,
    /// The tree to land; changes when the builder pushes onto the train.
    pub tree: u8,
    /// The tree the latest CI run started on.
    pub tested: Option<u8>,
    pub red_rounds: u8,
    /// The CI run in flight or last started; a result names the run it belongs to.
    pub run: u8,
    /// `VerifyComplete` accepted this exact membership, reasons and tree.
    pub checked: bool,
    /// Landed, closed by a red CI, or discarded.
    pub closed: bool,
    pub landed: bool,
}

/// How a train reaches main (rule 3.44).
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Landing {
    /// Main moves to exactly the tested commit.
    TestedFf,
    /// Main receives a wrapper commit: a speculative base never equals main, replay and shadow only.
    PrMerge,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    /// Ordinary trains take at most this many parts.
    pub target: u8,
    /// The hard limit of a train.
    pub cap: u8,
    /// An overlapping part is deferred while it has waited less than this (ticks); then it rides.
    pub bound: u8,
    /// An ordinary train is due when a part has waited this long or the target is reached.
    pub cut_after: u8,
    /// Policy (a): a part left out this many times as Overlap or Target is due.
    pub due_after: u8,
    pub order: Order,
    pub landing: Landing,
}

impl Policy {
    pub fn new(target: u8, bound: u8, cut_after: u8, order: Order) -> Policy {
        Policy {
            target,
            cap: u8::MAX,
            bound,
            cut_after,
            due_after: 2,
            order,
            landing: Landing::TestedFf,
        }
    }
    /// Rule 3.74 (1): every ready part rides; only a missing verified parent holds a part back.
    pub fn every_ready_rides(order: Order) -> Policy {
        Policy::new(u8::MAX, 0, 0, order)
    }
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
    /// A result of CI run `run`; a result of an older run is refused.
    CiResult {
        train: TrainId,
        run: u8,
        green: bool,
    },
    /// The CI log names the failing part: it and its descendants drop, the rest are retried as one group.
    CiRed {
        train: TrainId,
        run: u8,
        culprit: PartId,
    },
    /// A red round the builder will fix in place; refused from the second red round on.
    CiRedFix {
        train: TrainId,
        run: u8,
    },
    /// The fix of a red train is pushed: a new tree that needs a fresh bind and a full CI run.
    FixInPlace {
        train: TrainId,
    },
    /// A change is pushed onto a train after its CI ran: the tested tree no longer is the tree.
    Amend {
        train: TrainId,
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
    /// The resolution of a conflicting part is adopted: it and what waited on it ride again; refused
    /// once CI has started.
    Adopt {
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
    /// The builder restarts: what is in the landing journal stays, an open train must be bound again.
    Restart,
    /// Build the next candidate on the unlanded `pred` and test it meanwhile.
    Speculate {
        pred: TrainId,
    },
    /// Land a speculative candidate; only in tested fast-forward, only on its exact predecessor.
    Promote {
        train: TrainId,
    },
    /// The registrar records why `part` is not in `train`; the verifier is the check.
    RecordLeftOut {
        train: TrainId,
        part: PartId,
        disposition: Disposition,
    },
    /// A defective builder loses the record of one left-out part.
    OmitLeftOut {
        train: TrainId,
        part: PartId,
    },
    /// The verifier checks one recorded disposition.
    VerifyLeftOut {
        train: TrainId,
        part: PartId,
    },
    /// The verifier checks every recorded reason and that every part that was ready has its place or
    /// its reason; CI and settlement need this result for the train as it stands.
    VerifyComplete {
        train: TrainId,
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
    /// A speculative candidate was thrown away; its parts are ready again.
    Discarded,
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
    /// A fix in place after two red rounds.
    TwoRounds,
    /// The tree to land is not the tree CI ran on.
    TreeChanged,
    /// A speculative candidate is not landing evidence in this mode.
    Mode,
    /// A CI result of an older run.
    StaleRun,
    /// The train's membership, reasons and tree were not verified complete.
    Unverified,
}

impl Train {
    /// A train that is open: not bound, CI not started.
    pub(crate) fn new(kind: Kind, parts: Vec<PartId>, base: u8) -> Train {
        Train {
            kind,
            included: Some(parts.clone()),
            ready_at_cut: parts.clone(),
            parts,
            left_out: Vec::new(),
            conflicted: Vec::new(),
            base,
            bound: false,
            sealed: false,
            frozen: Vec::new(),
            ci: Ci::NotStarted,
            tree: 0,
            tested: None,
            red_rounds: 0,
            run: 0,
            checked: false,
            closed: false,
            landed: false,
        }
    }

    /// Bound and testing from the start: a retry group or a speculative candidate.
    pub(crate) fn started(mut self) -> Train {
        self.bound = true;
        self.sealed = true;
        self.frozen = self.parts.clone();
        self.ci = Ci::Running;
        self.tested = Some(self.tree);
        self.run = 1;
        self.checked = true;
        self
    }
}
