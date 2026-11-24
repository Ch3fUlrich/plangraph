//! The trusted verifier of a computed GO (LAND-GATE-COMPUTED-GO).
//!
//! `verify` is pure: it takes the evidence read from protected sources (the repository ref, the CI jobs of
//! the workflow run, the approvals, the delta review receipt, the replay of the train and the state of
//! main) and the pins compiled into this binary (the workflow definition, the expected job matrix, the
//! operator-class paths). It binds every piece to one repository, pull request, base, head, tree,
//! manifest, workflow run and attempt, and returns every refusal it finds, not only the first.
use super::main_gh::{matrix_from_toml, MATRIX};
use super::main_state::{Matrix, State};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

/// The workflow whose jobs are the CI evidence; its text is part of this binary.
pub const WORKFLOW_PATH: &str = ".github/workflows/tests.yml";
const WORKFLOW: &str = include_str!("../../../../.github/workflows/tests.yml");
/// The event of the run whose jobs count: the full run of the pull request.
pub const EVENT: &str = "pull_request";
/// Paths only an operator-granted `Gate-Edit` may change (a prefix, or a file name at any depth for `*/`).
pub const OPERATOR_CLASS: [&str; 6] = [
    "tools/ci_commit_gate.py",
    "tools/ci_commit_exempt.txt",
    ".github/workflows/",
    "tools/gate_wiring_check.py",
    ".gitattributes",
    "deploy/ci-watch/matrix.toml",
];

pub struct Pins {
    pub workflow_sha256: String,
    pub matrix: Matrix,
}

impl Pins {
    /// The pins of this binary; a changed workflow or matrix changes the pins only by a rebuild.
    pub fn protected() -> Result<Pins, String> {
        Ok(Pins {
            workflow_sha256: format!("{:x}", Sha256::digest(WORKFLOW.as_bytes())),
            matrix: matrix_from_toml(MATRIX)?,
        })
    }
}

/// The repository as the protected source shows it now.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Observed {
    pub repo: String,
    pub pr: u64,
    pub pr_head: String,
    pub pr_base: String,
    pub main_tip: String,
}

/// The result of `land-train verify` on the exact train.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Replay {
    pub verified: bool,
    pub base: String,
    pub train_sha: String,
    pub manifest: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct GoJob {
    pub name: String,
    pub conclusion: Option<String>,
    /// The attempt this job row belongs to.
    pub attempt: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ci {
    pub repo: String,
    pub workflow: String,
    /// SHA-256 of the workflow file at the head under test.
    pub workflow_sha256: String,
    pub event: String,
    pub run_id: u64,
    pub attempt: u32,
    /// The highest attempt number the run has.
    pub latest_attempt: u32,
    pub head: String,
    pub tree: String,
    /// Runner tier of the attempt: `self` or `hosted`.
    pub tier: String,
    pub jobs: Vec<GoJob>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Approval {
    pub d_id: String,
    pub repo: String,
    pub pr: u64,
    pub head: String,
    /// The standalone `MIGRATION <version>` field of the GO statement, when it has one.
    #[serde(default)]
    pub migration: Option<u32>,
}

/// The keeper schema version at the train base and at its head (`land-train keeper-schema`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct KeeperSchema {
    pub base: u32,
    pub head: u32,
}

/// The accepted review of the train delta, as `land-train admit` proved it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeltaAcceptance {
    pub run_id: String,
    pub head: String,
    pub manifest: String,
    pub accepted: bool,
}

/// An operator's `Gate-Edit` grant for one path prefix of one exact head.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Grant {
    pub d_id: String,
    pub prefix: String,
    pub head: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Evidence {
    pub repo: String,
    pub pr: u64,
    pub base: String,
    pub head: String,
    pub tree: String,
    pub manifest: String,
    pub observed: Observed,
    pub replay: Replay,
    pub ci: Ci,
    pub approvals: Vec<Approval>,
    pub delta: Option<DeltaAcceptance>,
    pub changed_paths: Vec<String>,
    pub grants: Vec<Grant>,
    pub main: State,
    /// Required, so evidence that does not state the schema versions is refused as unreadable.
    pub keeper_schema: KeeperSchema,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Refusal {
    pub code: &'static str,
    pub detail: String,
}

fn no(code: &'static str, detail: impl Into<String>) -> Refusal {
    Refusal {
        code,
        detail: detail.into(),
    }
}

fn operator_class(path: &str) -> bool {
    OPERATOR_CLASS.iter().any(|p| match p.strip_suffix('/') {
        Some(_) => path.starts_with(p),
        None => {
            path == *p || path.starts_with(&format!("{p}/")) || path.ends_with(&format!("/{p}"))
        }
    })
}

fn granted(e: &Evidence, path: &str) -> bool {
    e.grants.iter().any(|g| {
        g.head == e.head
            && valid_d_id(&g.d_id)
            && (path == g.prefix || (g.prefix.ends_with('/') && path.starts_with(&g.prefix)))
    })
}

fn valid_d_id(id: &str) -> bool {
    id.strip_prefix("D-")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

fn migration_approved(e: &Evidence) -> bool {
    e.approvals.iter().any(|a| {
        a.repo == e.repo
            && a.pr == e.pr
            && a.head == e.head
            && valid_d_id(&a.d_id)
            && a.migration == Some(e.keeper_schema.head)
    })
}

/// Every reason the evidence does not prove a GO; empty when it does.
pub fn verify(e: &Evidence, pins: &Pins) -> Vec<Refusal> {
    let mut out = Vec::new();
    let o = &e.observed;
    if o.repo != e.repo || o.pr != e.pr {
        out.push(no(
            "wrong_repository",
            format!(
                "evidence is for {} #{}, the ref shows {} #{}",
                e.repo, e.pr, o.repo, o.pr
            ),
        ));
    }
    if o.pr_head != e.head {
        out.push(no(
            "moved_head",
            format!(
                "the pull request head is {}, the evidence is for {}",
                o.pr_head, e.head
            ),
        ));
    }
    if o.main_tip != e.base || o.pr_base != e.base {
        out.push(no(
            "stale_base",
            format!(
                "main is at {}, the pull request base {}, the train base {}",
                o.main_tip, o.pr_base, e.base
            ),
        ));
    }
    let r = &e.replay;
    if !r.verified || r.base != e.base || r.train_sha != e.head || r.manifest != e.manifest {
        out.push(no(
            "replay_mismatch",
            "the replay did not verify this base, head and manifest",
        ));
    }
    ci(e, pins, &mut out);
    if !e
        .approvals
        .iter()
        .any(|a| a.repo == e.repo && a.pr == e.pr && a.head == e.head && valid_d_id(&a.d_id))
    {
        out.push(no(
            "approval_missing",
            "no GO approval names this repository, pull request and head",
        ));
    }
    match &e.delta {
        Some(d)
            if d.accepted
                && d.head == e.head
                && d.manifest == e.manifest
                && pg_worker_core::run::record::valid_run_id(&d.run_id) => {}
        Some(d) if d.head != e.head || d.manifest != e.manifest => out.push(no(
            "delta_unbound",
            "the delta acceptance is for another head or manifest",
        )),
        _ => out.push(no("delta_missing", "no accepted review of the train delta")),
    }
    for path in e.changed_paths.iter().filter(|p| operator_class(p)) {
        if !granted(e, path) {
            out.push(no(
                "operator_path_unauthorized",
                format!("{path} needs a Gate-Edit grant for this head"),
            ));
        }
    }
    let schema = &e.keeper_schema;
    if schema.head < schema.base {
        out.push(no(
            "migration_regressed",
            format!("keeper schema goes from {} to {}", schema.base, schema.head),
        ));
    } else if schema.head > schema.base && !migration_approved(e) {
        out.push(no(
            "migration_missing",
            format!(
                "keeper schema rises {} -> {}: the GO for this head needs `MIGRATION {}`, and the deploy is `plangraph keeper-restart`, not a plain restart",
                schema.base, schema.head, schema.head
            ),
        ));
    }
    if let Some(word) = e.main.refusal() {
        out.push(no("main_not_green", word));
    }
    out
}

fn ci(e: &Evidence, pins: &Pins, out: &mut Vec<Refusal>) {
    let c = &e.ci;
    if c.repo != e.repo || c.head != e.head || c.tree != e.tree {
        out.push(no(
            "ci_unbound",
            "the run is for another repository, head or tree",
        ));
    }
    if c.workflow != WORKFLOW_PATH || c.event != EVENT || c.workflow_sha256 != pins.workflow_sha256
    {
        out.push(no(
            "workflow_unpinned",
            "the run is not the pinned workflow, event and definition",
        ));
    }
    if c.attempt != c.latest_attempt {
        out.push(no(
            "stale_attempt",
            format!(
                "attempt {} is not the latest, {}",
                c.attempt, c.latest_attempt
            ),
        ));
    }
    if let Some(job) = c.jobs.iter().find(|j| j.attempt != c.attempt) {
        out.push(no(
            "mixed_attempt",
            format!(
                "job {:?} ran in attempt {}, the run is judged at {}",
                job.name, job.attempt, c.attempt
            ),
        ));
    }
    let Some(expected) = pins.matrix.tiers.get(&c.tier).filter(|_| c.tier != "fast") else {
        out.push(no(
            "tier_unknown",
            format!("tier {:?} has no pinned full matrix", c.tier),
        ));
        return;
    };
    let mut seen: BTreeMap<&str, bool> = BTreeMap::new();
    for j in &c.jobs {
        *seen.entry(j.name.as_str()).or_insert(true) &= j.conclusion.as_deref() == Some("success");
    }
    for name in expected.keys() {
        match seen.get(name.as_str()) {
            None => out.push(no("job_missing", name.clone())),
            Some(false) => out.push(no("job_not_green", name.clone())),
            Some(true) => {}
        }
    }
}

/// `land-train go-verify --evidence FILE`: prints one `GO-VERIFIED` line, or refuses with every reason.
pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let path = flags.get("evidence").ok_or("missing --evidence")?;
    let e: Evidence =
        serde_json::from_slice(&std::fs::read(path).map_err(|x| format!("{path}: {x}"))?)
            .map_err(|x| format!("{path}: {x}"))?;
    let refusals = verify(&e, &Pins::protected()?);
    if !refusals.is_empty() {
        let lines: Vec<_> = refusals
            .iter()
            .map(|r| format!("{}: {}", r.code, r.detail))
            .collect();
        return Err(format!("go refused:\n{}", lines.join("\n")));
    }
    Ok(vec![format!(
        "GO-VERIFIED {} #{} base {} head {} tree {} manifest {} run {} attempt {}",
        e.repo, e.pr, e.base, e.head, e.tree, e.manifest, e.ci.run_id, e.ci.attempt
    )])
}
