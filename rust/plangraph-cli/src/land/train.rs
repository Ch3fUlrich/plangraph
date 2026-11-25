//! The landing train as data: parts, dependency closure (P3), the manifest, the build record (P4, P9)
//! and the freeze comparison. Pure; the git-backed builder and verifier are in `build`.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

use super::main_state::State;

/// The parent a part was registered on: the parent pull request and its READY head at that moment.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Parent {
    pub pr: u64,
    pub head: String,
}

/// One READY part as the registrar hands it over: pull request, head, declared parent.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PartIn {
    pub pr: u64,
    pub head: String,
    pub parent: Option<Parent>,
    pub receipt: PartReceipt,
}

/// Reference to the passing seat checked by the READY registrar for this part.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PartReceipt {
    pub run_id: String,
    pub head: String,
    /// The receipt's ACCEPT and part CI carried over a clean rebase to this part's head.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub carried: Option<Carry>,
}

/// A carry-over (`evidence-assess --carry-out`): the evidence of `old_head` and the head it was asked to
/// carry to. The builder re-assesses it against the base it builds on before it takes the receipt.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Carry {
    pub old_head: String,
    pub new_head: String,
    pub evidence: super::evidence::Evidence,
}

impl PartIn {
    pub(super) fn check_receipt(&self) -> Result<(), String> {
        let carried = self
            .receipt
            .carried
            .as_ref()
            .is_some_and(|c| c.old_head == self.receipt.head && c.new_head == self.head);
        if (self.receipt.head != self.head && !carried)
            || !pg_worker_core::run::record::valid_run_id(&self.receipt.run_id)
        {
            return Err(format!(
                "receipt: part #{} head {} differs from receipt head {} or the run ID is invalid",
                self.pr, self.head, self.receipt.head
            ));
        }
        Ok(())
    }
}

/// The digest and count of the commits of `diff_base..head`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Own {
    pub count: usize,
    pub digest: String,
}

impl Own {
    pub fn of(commits: &[String]) -> Own {
        let mut sorted: Vec<&String> = commits.iter().collect();
        sorted.sort();
        let text = sorted
            .iter()
            .map(|c| c.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        Own {
            count: commits.len(),
            digest: format!("{:x}", Sha256::digest(text.as_bytes())),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Part {
    pub pr: u64,
    pub head: String,
    pub parent: Option<Parent>,
    pub own: Own,
    pub receipt: PartReceipt,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Reason {
    /// The merge of the part's head onto its prefix conflicts under the protected inputs.
    Conflict,
    /// A part below it in its chain is not merged in this build.
    Descendant,
    /// The train is at its size cap; the stack enters as a dependency-closed prefix.
    Cap,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeftOut {
    pub pr: u64,
    pub head: String,
    pub reason: Reason,
    /// For `conflict`: the commit the merge was attempted on.
    pub prefix: String,
}

/// The merge inputs fixed by the policy version and recorded with the train.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Policy {
    pub version: String,
    pub git: String,
    /// The merge attributes used (the builder accepts them from outside the tree).
    pub attributes: String,
}

/// A conflicting part merged by an adopted resolution instead of being left out: the part, the adopted
/// submission and the merge commit. It must match an adopted result of the freeze's build.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Resolution {
    pub part: u64,
    pub submission: String,
    pub commit: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    pub build: String,
    pub policy: Policy,
    pub base: String,
    /// Parts in stack order.
    pub parts: Vec<Part>,
    /// (child pr, parent pr) edges among the parts.
    pub graph: Vec<(u64, u64)>,
    /// Every READY head left out, with its reason.
    pub left_out: Vec<LeftOut>,
    /// The merge commit of each part, in order.
    pub merges: Vec<String>,
    /// Parts whose merge commit is an adopted resolution.
    #[serde(default)]
    pub resolution: Vec<Resolution>,
    pub train_sha: String,
}

/// `land_build_open`: fixed before the builder merges anything; the freeze must carry the same three.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct BuildOpen {
    pub build: String,
    pub policy_version: String,
    pub base: String,
    pub grant_digest: String,
    /// Position Nb of the log prefix the selection snapshot was computed over.
    pub snapshot_position: u64,
}

/// What the push gate computes at the freeze.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Freeze {
    pub policy_version: String,
    pub base: String,
    pub grant_digest: String,
    pub selection: Vec<PartIn>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum FreezeRefusal {
    PolicyVersion,
    Base,
    GrantDigest,
    /// The two full selections differ (a part became READY, unready or got another head or dependency).
    SelectionChanged,
}

/// A selection without its admission evidence: a carried receipt says how the receipt was admitted at
/// this build, not which parts were selected, so it is not part of what a freeze must equal.
pub fn selection_identity(parts: &[PartIn]) -> Vec<PartIn> {
    parts
        .iter()
        .cloned()
        .map(|mut p| {
            p.receipt.carried = None;
            p
        })
        .collect()
}

/// The freeze compares the full selections before any exclusion is applied.
pub fn check_freeze(
    open: &BuildOpen,
    snapshot: &[PartIn],
    freeze: &Freeze,
) -> Result<(), FreezeRefusal> {
    if freeze.policy_version != open.policy_version {
        return Err(FreezeRefusal::PolicyVersion);
    }
    if freeze.base != open.base {
        return Err(FreezeRefusal::Base);
    }
    if freeze.grant_digest != open.grant_digest {
        return Err(FreezeRefusal::GrantDigest);
    }
    if selection_identity(&freeze.selection) != selection_identity(snapshot) {
        return Err(FreezeRefusal::SelectionChanged);
    }
    Ok(())
}

/// No train is built while main is red or pending (design D5): `main_red`, `main_pending`, ...
pub fn require_open_main(state: &State) -> Result<(), &'static str> {
    state.refusal().map_or(Ok(()), Err)
}

/// Stack order: every part after the part it is stacked on, otherwise the given order. Parts on a parent
/// cycle are returned last; the closure leaves them out.
pub fn stack_order(parts: &[PartIn]) -> Vec<PartIn> {
    let prs: BTreeSet<u64> = parts.iter().map(|p| p.pr).collect();
    let mut done: BTreeSet<u64> = BTreeSet::new();
    let mut out: Vec<PartIn> = Vec::new();
    loop {
        let before = out.len();
        for p in parts {
            if done.contains(&p.pr) {
                continue;
            }
            let ready = p
                .parent
                .as_ref()
                .is_none_or(|q| !prs.contains(&q.pr) || done.contains(&q.pr));
            if ready {
                done.insert(p.pr);
                out.push(p.clone());
            }
        }
        if out.len() == before {
            break;
        }
    }
    out.extend(parts.iter().filter(|p| !done.contains(&p.pr)).cloned());
    out
}

/// Whether a part may enter: its parent is in the manifest before it with exactly the registered head,
/// or that head already is in the train base (the parent landed).
pub fn includable(
    part: &PartIn,
    merged: &BTreeMap<u64, String>,
    in_base: &dyn Fn(&str) -> bool,
) -> bool {
    match &part.parent {
        None => true,
        Some(q) => merged.get(&q.pr) == Some(&q.head) || in_base(&q.head),
    }
}

/// Counts verified `conflict` exclusions per head; the third in a row escalates to plan-order once.
#[derive(Default, Serialize, Deserialize)]
pub struct ConflictCounter {
    streak: BTreeMap<String, u32>,
    seen_builds: BTreeSet<String>,
    escalated: BTreeSet<String>,
}

impl ConflictCounter {
    /// Records one verified build; returns the stable id of each new escalation (head + third build id).
    pub fn record(&mut self, build: &str, left_out: &[LeftOut]) -> Vec<String> {
        if !self.seen_builds.insert(build.to_owned()) {
            return vec![];
        }
        let conflicted: BTreeSet<&str> = left_out
            .iter()
            .filter(|l| l.reason == Reason::Conflict)
            .map(|l| l.head.as_str())
            .collect();
        self.streak
            .retain(|head, _| conflicted.contains(head.as_str()));
        let mut out = vec![];
        for head in conflicted {
            let n = self.streak.entry(head.to_owned()).or_insert(0);
            *n += 1;
            if *n == 3 {
                let id = format!("{head}:{build}");
                if self.escalated.insert(id.clone()) {
                    out.push(id);
                }
            }
        }
        out
    }
}
