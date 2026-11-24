//! Evidence that survives a clean rebase (D-6742, D-6743): a batch's review ACCEPT and part CI stay valid
//! while its own patch is unchanged and main did not touch its paths. Pure; the git facts come from
//! `evidence_git`. A flake verdict decides only what voids an ACCEPT, never what may merge.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

use super::main_state::{admit, State};

/// Trains that may land after the evidence base before the part CI runs again; the ACCEPT is not bounded.
pub const TRAIN_BOUND: u64 = 3;

/// What the evidence is about: the patch-id of the batch's own diff and the paths it touches.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Key {
    pub patch_id: String,
    pub paths: BTreeSet<String>,
}

/// The ACCEPT and part CI of one head, with the main they were taken on.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Evidence {
    pub key: Key,
    pub base: String,
    pub head: String,
    /// Trains landed on main when the evidence was taken.
    pub trains_at_base: u64,
    pub accept: bool,
    pub ci_green: bool,
}

/// What a rebase or a main move produced, read from the repository.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Facts {
    pub key_now: Key,
    pub new_base: String,
    pub new_head: String,
    /// Paths changed on main since the evidence base.
    pub main_paths: BTreeSet<String>,
    /// The merge of the head onto main conflicts textually.
    pub conflict: bool,
    pub trains_now: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Void {
    Conflict,
    PatchIdChanged,
    PathsChanged,
    MainTouched(BTreeSet<String>),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReReview {
    /// Only the range-diff old head to new head: a resolution or a changed patch.
    RangeDiff { old_head: String, new_head: String },
    /// Main changed what the batch touches without a textual conflict: the whole batch again.
    Full,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Accept {
    Carried {
        old_head: String,
        new_head: String,
    },
    Void {
        reason: Void,
        re_review: ReReview,
    },
    /// There was no ACCEPT to carry.
    None,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CiRerun {
    Void(Void),
    TrainBound { landed: u64, bound: u64 },
    NoPassingCi,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Ci {
    Carried { old_head: String, new_head: String },
    Rerun(CiRerun),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    pub accept: Accept,
    pub ci: Ci,
}

fn void_of(e: &Evidence, f: &Facts) -> Option<Void> {
    if f.conflict {
        return Some(Void::Conflict);
    }
    if f.key_now.patch_id != e.key.patch_id {
        return Some(Void::PatchIdChanged);
    }
    if f.key_now.paths != e.key.paths {
        return Some(Void::PathsChanged);
    }
    let touched: BTreeSet<String> = f.main_paths.intersection(&e.key.paths).cloned().collect();
    (!touched.is_empty()).then_some(Void::MainTouched(touched))
}

/// Whether the ACCEPT and the part CI carry to the new head. Nothing is rebased for freshness: a conflict
/// is the only reason a rebase happens, and then only the resolution is re-reviewed.
pub fn decide(e: &Evidence, f: &Facts) -> Decision {
    let (old_head, new_head) = (e.head.clone(), f.new_head.clone());
    if let Some(reason) = void_of(e, f) {
        let re_review = match reason {
            Void::Conflict | Void::PatchIdChanged => ReReview::RangeDiff { old_head, new_head },
            Void::PathsChanged | Void::MainTouched(_) => ReReview::Full,
        };
        // With no ACCEPT there is nothing to void and nothing to re-review.
        let accept = if e.accept {
            Accept::Void {
                reason: reason.clone(),
                re_review,
            }
        } else {
            Accept::None
        };
        return Decision {
            accept,
            ci: Ci::Rerun(CiRerun::Void(reason)),
        };
    }
    let landed = f.trains_now.saturating_sub(e.trains_at_base);
    let ci = if !e.ci_green {
        Ci::Rerun(CiRerun::NoPassingCi)
    } else if landed > TRAIN_BOUND {
        Ci::Rerun(CiRerun::TrainBound {
            landed,
            bound: TRAIN_BOUND,
        })
    } else {
        Ci::Carried {
            old_head: old_head.clone(),
            new_head: new_head.clone(),
        }
    };
    let accept = if e.accept {
        Accept::Carried { old_head, new_head }
    } else {
        Accept::None
    };
    Decision { accept, ci }
}

/// A red CI run: the failing tests, those that also fail without the batch's patch, and hung ones.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Red {
    pub failing: BTreeSet<String>,
    pub failing_without_patch: BTreeSet<String>,
    pub hung: BTreeSet<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RedKind {
    Genuine,
    Flake,
}

/// A red is a flake when every failing test also fails without the patch or hung. Unknown is genuine.
pub fn classify_red(r: &Red) -> RedKind {
    let flake = !r.failing.is_empty()
        && r.failing
            .iter()
            .all(|t| r.failing_without_patch.contains(t) || r.hung.contains(t));
    if flake {
        RedKind::Flake
    } else {
        RedKind::Genuine
    }
}

/// Only a genuine red voids a carried ACCEPT.
pub fn voids_accept(kind: RedKind) -> bool {
    kind == RedKind::Genuine
}

/// The merge gate: a green required CI and a green main. It takes no flake input: main is never red.
pub fn may_merge(required_ci_green: bool, main: &State) -> Result<(), &'static str> {
    if !required_ci_green {
        return Err("required_ci_not_green");
    }
    admit(main, None)
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Kind {
    Rebase,
    AcceptCarried { old_head: String, new_head: String },
    CiCarried { old_head: String, new_head: String },
    ReReview,
    FixRoundAfterRebase,
}

/// One line of the evidence log.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Record {
    pub day: String,
    #[serde(flatten)]
    pub kind: Kind,
}

impl Record {
    pub fn new(day: &str, kind: Kind) -> Record {
        Record {
            day: day.to_owned(),
            kind,
        }
    }
}

/// The log lines one rebase decision produces.
pub fn records_of(d: &Decision, day: &str) -> Vec<Record> {
    let mut out = vec![Record::new(day, Kind::Rebase)];
    match &d.accept {
        Accept::Carried { old_head, new_head } => out.push(Record::new(
            day,
            Kind::AcceptCarried {
                old_head: old_head.clone(),
                new_head: new_head.clone(),
            },
        )),
        Accept::Void { .. } => out.push(Record::new(day, Kind::ReReview)),
        Accept::None => {}
    }
    if let Ci::Carried { old_head, new_head } = &d.ci {
        out.push(Record::new(
            day,
            Kind::CiCarried {
                old_head: old_head.clone(),
                new_head: new_head.clone(),
            },
        ));
    }
    out
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayReport {
    pub rebases: u64,
    pub accepts_carried: u64,
    pub ci_carried: u64,
    pub re_reviews: u64,
    pub fix_rounds_after_rebase: u64,
}

/// Counts per day. A carry that does not name both heads is refused: the evidence trail must be complete.
pub fn daily(records: &[Record]) -> Result<BTreeMap<String, DayReport>, String> {
    let mut days: BTreeMap<String, DayReport> = BTreeMap::new();
    for r in records {
        let d = days.entry(r.day.clone()).or_default();
        match &r.kind {
            Kind::Rebase => d.rebases += 1,
            Kind::AcceptCarried { old_head, new_head } | Kind::CiCarried { old_head, new_head } => {
                if old_head.is_empty() || new_head.is_empty() {
                    return Err(format!(
                        "evidence record of {} carries without naming the old and the new head",
                        r.day
                    ));
                }
                if matches!(r.kind, Kind::AcceptCarried { .. }) {
                    d.accepts_carried += 1;
                } else {
                    d.ci_carried += 1;
                }
            }
            Kind::ReReview => d.re_reviews += 1,
            Kind::FixRoundAfterRebase => d.fix_rounds_after_rebase += 1,
        }
    }
    Ok(days)
}
