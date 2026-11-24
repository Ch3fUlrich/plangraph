//! The collector of the go-verify evidence (LAND-GATE-COMPUTED-GO).
//!
//! `collect` builds the `Evidence` of `go_verify` from protected sources only: the pull request ref, the
//! `tests.yml` run of the train head with the jobs of the attempt it judges, the main tip and its
//! state, the GO and Gate-Edit rows of the decisions log, and the receipt `land-train admit` wrote. A
//! caller never writes the evidence. The pull request is read twice, before and after every other read;
//! a head that differs between the two reads is refused.
#[path = "go_evidence_rows.rs"]
mod rows_source;

use super::go_verify::{
    Ci, DeltaAcceptance, Evidence, GoJob, Observed, Replay, EVENT, WORKFLOW_PATH,
};
use super::main_gh::{ci_publisher, tested_attempts, tier_of};
use super::main_state::{Answer, Job};
use crate::ci_watch::github::Github;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// What `land-train admit` proved about the train: the replay verified and the delta review accepted.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdmitReceipt {
    pub replay: Replay,
    pub delta: Option<DeltaAcceptance>,
}

/// The receipt of a train that passed `admit` with review run `run_id`.
pub fn admit_receipt(base: &str, train_sha: &str, manifest: &str, run_id: &str) -> AdmitReceipt {
    AdmitReceipt {
        replay: Replay {
            verified: true,
            base: base.into(),
            train_sha: train_sha.into(),
            manifest: manifest.into(),
        },
        delta: Some(DeltaAcceptance {
            run_id: run_id.into(),
            head: train_sha.into(),
            manifest: manifest.into(),
            accepted: true,
        }),
    }
}

/// SHA-256 of the manifest file: the identity every part of the evidence binds to.
pub fn manifest_digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
pub(in crate::land) use rows_source::read_decisions_origin;

pub struct Inputs<'a> {
    pub repo: &'a str,
    pub pr: u64,
    pub base: &'a str,
    pub train_sha: &'a str,
    pub manifest: &'a str,
    pub receipt: &'a AdmitReceipt,
    /// The decisions log (`| D-id | project | decision | by | why |` rows).
    pub rows: &'a str,
    /// The state of main as `main-state` read it.
    pub main: &'a Answer,
}

/// Standard base64, whitespace ignored.
pub(super) fn decode_base64(text: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0);
    for c in text
        .bytes()
        .filter(|c| !c.is_ascii_whitespace() && *c != b'=')
    {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return Err("invalid base64".into()),
        };
        acc = acc << 6 | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Ok(out)
}

fn text(v: &Value, what: &str) -> Result<String, String> {
    v.as_str()
        .map(str::to_owned)
        .ok_or_else(|| format!("the source gave no {what}"))
}

/// The repository and head the pull request shows now.
fn pull(gh: &mut impl Github, pr: u64) -> Result<(u64, String, String), String> {
    let v = gh.value(&format!("pulls/{pr}"))?;
    Ok((
        v["number"]
            .as_u64()
            .ok_or("the pull request has no number")?,
        text(&v["base"]["repo"]["full_name"], "repository")?,
        text(&v["head"]["sha"], "pull request head")?,
    ))
}

fn paths_between(gh: &mut impl Github, from: &str, to: &str) -> Result<Vec<String>, String> {
    let rows = gh.pages(
        &format!("compare/{from}...{to}?per_page=100"),
        Some("files"),
    )?;
    // A rename names both ends: moving a protected file away is a change of the protected path.
    let mut paths = Vec::new();
    for file in &rows {
        for field in ["filename", "previous_filename"] {
            if let Some(path) = file[field].as_str() {
                paths.push(path.to_owned());
            }
        }
    }
    Ok(paths)
}

fn ci(gh: &mut impl Github, repo: &str, pr: u64, head: &str, tree: &str) -> Result<Ci, String> {
    let runs = gh.page(
        &format!("actions/workflows/tests.yml/runs?head_sha={head}&event={EVENT}&per_page=100"),
        Some("workflow_runs"),
    )?;
    let own = |r: &&Value| {
        r["head_sha"] == head
            && r["event"] == EVENT
            && r["path"]
                .as_str()
                .is_some_and(|p| p.split('@').next() == Some(WORKFLOW_PATH))
            && r["pull_requests"]
                .as_array()
                .is_some_and(|prs| prs.iter().any(|p| p["number"].as_u64() == Some(pr)))
    };
    let run = runs
        .iter()
        .filter(own)
        .max_by_key(|r| r["id"].as_u64())
        .ok_or("no tests.yml pull_request run for the train head")?;
    let id = run["id"].as_u64().ok_or("the run has no id")?;
    let attempt = run["run_attempt"].as_u64().unwrap_or(1) as u32;
    // The run tested a merge ref of its own: only a `ci-full` receipt of the CI publisher ties this attempt to
    // the tree of the train.
    let statuses = gh.page(&format!("commits/{head}/statuses?per_page=100"), None)?;
    let tested = tested_attempts(&statuses, tree, ci_publisher());
    if !tested
        .get(&id)
        .and_then(|attempts| attempts.get(&attempt))
        .is_some_and(|(_, success)| *success)
    {
        return Err(format!(
            "ci_unbound: no successful ci-full receipt ties run {id} attempt {attempt} to tree {tree}"
        ));
    }
    let rows = gh.pages(
        &format!("actions/runs/{id}/attempts/{attempt}/jobs?per_page=100"),
        Some("jobs"),
    )?;
    let jobs: Vec<GoJob> = rows
        .iter()
        .map(|j| GoJob {
            name: j["name"].as_str().unwrap_or_default().to_owned(),
            conclusion: j["conclusion"].as_str().map(str::to_owned),
            attempt: j["run_attempt"].as_u64().map_or(attempt, |n| n as u32),
        })
        .collect();
    // The newest attempt after the jobs were read: a rerun in between makes the judged one stale.
    let latest_attempt = gh.value(&format!("actions/runs/{id}"))?["run_attempt"]
        .as_u64()
        .map_or(attempt, |n| n as u32);
    let file = gh.value(&format!("contents/{WORKFLOW_PATH}?ref={head}"))?;
    let workflow = decode_base64(file["content"].as_str().unwrap_or_default())?;
    let seen: Vec<Job> = jobs
        .iter()
        .map(|j| Job {
            name: j.name.clone(),
            conclusion: j.conclusion.clone(),
            partial_log: false,
            tests: vec![],
        })
        .collect();
    Ok(Ci {
        repo: repo.into(),
        workflow: WORKFLOW_PATH.into(),
        workflow_sha256: format!("{:x}", Sha256::digest(&workflow)),
        event: EVENT.into(),
        run_id: id,
        attempt,
        latest_attempt,
        head: head.into(),
        tree: tree.into(),
        tier: tier_of(&seen),
        jobs,
    })
}

/// The evidence of a GO for `i.pr`, read from the sources; the verifier judges it.
pub fn collect(gh: &mut impl Github, i: &Inputs) -> Result<Evidence, String> {
    let (number, repo, pr_head) = pull(gh, i.pr)?;
    let tip = text(
        &gh.value("git/ref/heads/main")?["object"]["sha"],
        "main tip",
    )?;
    if tip != i.main.tip {
        return Err(format!(
            "stale_base: main is at {tip}, its state was read at {}",
            i.main.tip
        ));
    }
    let pr_base = text(
        &gh.value(&format!("compare/{tip}...{pr_head}"))?["merge_base_commit"]["sha"],
        "merge base",
    )?;
    let tree = text(
        &gh.value(&format!("git/commits/{}", i.train_sha))?["tree"]["sha"],
        "tree",
    )?;
    let changed_paths = paths_between(gh, i.base, i.train_sha)?;
    let ci = ci(gh, i.repo, i.pr, i.train_sha, &tree)?;
    let (approvals, grants) = rows_source::rows(i.rows, i.repo, i.pr);
    let (_, again_repo, again_head) = pull(gh, i.pr)?;
    if again_head != pr_head || again_repo != repo {
        return Err(format!(
            "moved_head: the pull request head was {pr_head} and is now {again_head}"
        ));
    }
    Ok(Evidence {
        repo: i.repo.into(),
        pr: i.pr,
        base: i.base.into(),
        head: i.train_sha.into(),
        tree,
        manifest: i.manifest.into(),
        observed: Observed {
            repo,
            pr: number,
            pr_head,
            pr_base,
            main_tip: tip,
        },
        replay: i.receipt.replay.clone(),
        ci,
        approvals,
        delta: i.receipt.delta.clone(),
        changed_paths,
        grants,
        main: i.main.state.clone(),
    })
}

/// The decisions document of the keeper configuration, read as it stands on `origin/main`.
fn decisions(config: &std::path::Path) -> Result<String, String> {
    let text = std::fs::read_to_string(config).map_err(|e| format!("{}: {e}", config.display()))?;
    let value: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let file = value["decisions_file"]
        .as_str()
        .ok_or("the keeper configuration names no decisions_file")?;
    rows_source::read_decisions_origin(std::path::Path::new(file))
}

/// The inputs that used to be files a caller wrote: the evidence reads them from protected sources now.
pub(super) fn reject_removed_flags(
    flags: &std::collections::BTreeMap<String, String>,
) -> Result<(), String> {
    for flag in ["rows", "receipt", "receipt-out"] {
        if flags.contains_key(flag) {
            return Err(format!(
                "--{flag} is gone: go-evidence reads the decisions from origin/main and re-runs the admit proof"
            ));
        }
    }
    Ok(())
}

/// `land-train go-evidence` after `cli` re-ran the admit proof: collects the evidence of the GO from GitHub,
/// the decisions on `origin/main` and that proof, and writes it for `go-verify`.
pub(super) fn run_cli(
    home: &std::path::Path,
    flags: &std::collections::BTreeMap<String, String>,
    manifest: &super::train::Manifest,
    manifest_bytes: &[u8],
    run_id: &str,
) -> Result<Vec<String>, String> {
    use super::{git, main_gh};
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let pr: u64 = need("pr")?.parse().map_err(|_| "pr must be a number")?;
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| home.join(".config"))
        .join("plangraph/keeper.json");
    let rows = decisions(&config)?;
    let origin = git::out(
        std::path::Path::new(need("repo")?),
        &["remote", "get-url", "origin"],
    )?;
    let slug = crate::ci_watch::github::repository(&origin)?;
    let (repo, ledger) = main_gh::repo_and_ledger(home, Some(&slug), "main")?;
    let matrix = main_gh::matrix_from_toml(main_gh::MATRIX)?;
    let main = main_gh::answer_for(repo.clone(), "main", ledger, &matrix);
    let digest = manifest_digest(manifest_bytes);
    let receipt = admit_receipt(&manifest.base, &manifest.train_sha, &digest, run_id);
    let evidence = collect(
        &mut crate::ci_watch::github::Gh { repo: repo.clone() },
        &Inputs {
            repo: &repo,
            pr,
            base: &manifest.base,
            train_sha: &manifest.train_sha,
            manifest: &digest,
            receipt: &receipt,
            rows: &rows,
            main: &main,
        },
    )?;
    let out = need("out")?;
    let body = serde_json::to_vec_pretty(&evidence).map_err(|e| e.to_string())?;
    pg_worker_core::run::record_io::atomic_write_private(std::path::Path::new(out), &body)
        .map_err(|e| format!("{out}: {e}"))?;
    Ok(vec![format!(
        "evidence {repo} #{pr} head {} run {} attempt {} written to {out}",
        evidence.head, evidence.ci.run_id, evidence.ci.attempt
    )])
}
