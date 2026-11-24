//! `plangraph land-train settle`: the real effects of [`super::settle`] (GitHub through `gh`, the keeper
//! through `plangraph merge-close`, the hosts through `plangraph landing verify-hosts`, git, and the
//! guarded `pg-spawnd writer-stop`) and the command that creates or resumes a settlement state file.
use super::settle::{Applied, Effects, HostReceipt, PartState, PrState, Proof, Settlement, Step};
use serde::Deserialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Command;

/// The landed manifest handed over once, by `--init`.
#[derive(Deserialize)]
struct Init {
    repo: String,
    train_pr: u64,
    merge: String,
    parts: Vec<InitPart>,
}

#[derive(Deserialize)]
struct InitPart {
    pr: u64,
    head: String,
    items: Vec<String>,
    folded: bool,
    #[serde(default)]
    writer_head: Option<String>,
    #[serde(default)]
    writer_record: Option<PathBuf>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    ready_at: Option<i64>,
}

fn full_sha(s: &str) -> bool {
    s.len() == 40 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

impl Init {
    fn into_settlement(self) -> Result<Settlement, String> {
        if !full_sha(&self.merge) {
            return Err("settle: merge must be a full lower-case SHA".into());
        }
        let mut seen = std::collections::BTreeSet::new();
        for p in &self.parts {
            if p.writer_head.is_some() != p.writer_record.is_some() {
                return Err(format!(
                    "settle: part #{} needs both writer_head and writer_record or neither",
                    p.pr
                ));
            }
        }
        let mut parts = Vec::new();
        for p in self.parts {
            if !full_sha(&p.head) || p.writer_head.as_deref().is_some_and(|w| !full_sha(w)) {
                return Err(format!("settle: part #{} needs full SHAs", p.pr));
            }
            if !seen.insert(p.pr) {
                return Err(format!("settle: part #{} listed twice", p.pr));
            }
            parts.push(PartState {
                pr: p.pr,
                head: p.head,
                items: p.items,
                folded: p.folded,
                writer_head: p.writer_head,
                writer_record: p.writer_record,
                owner: p.owner,
                ready_at: p.ready_at,
                close_items: Step::Pending,
                close_pr: Step::Pending,
                stop_writer: Step::Pending,
            });
        }
        Ok(Settlement::new(
            &self.repo,
            self.train_pr,
            &self.merge,
            parts,
        ))
    }
}

fn output(command: &mut Command, what: &str) -> Result<String, String> {
    let out = command.output().map_err(|e| format!("{what}: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "{what} failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    String::from_utf8(out.stdout).map_err(|_| format!("{what}: output is not UTF-8"))
}

pub(super) fn parse_pr_view(raw: &str) -> Result<(PrState, String), String> {
    let v: Value = serde_json::from_str(raw).map_err(|_| "invalid gh pr view output")?;
    let state = match v["state"].as_str() {
        Some("OPEN") => PrState::Open,
        Some("CLOSED") => PrState::Closed,
        Some("MERGED") => PrState::Merged,
        _ => return Err("gh pr view: unknown pull request state".into()),
    };
    let head = v["headRefOid"].as_str().ok_or("gh pr view: no head")?;
    Ok((state, head.to_owned()))
}

/// What `plangraph merge-close` printed about a part's items. The part stays held when the keeper
/// listed one as refused or flagged, found its deployment unproven, only previewed its close, or could
/// not scan a repository (then no close is claimed). An item that is in no row was closed by an earlier
/// pass or is in a state the automatic close does not touch.
pub(super) fn parse_close_report(report: &str, items: &[String]) -> Applied {
    for line in report.lines() {
        let mut fields = line.split('\t');
        let (Some(label), Some(key)) = (fields.next(), fields.next()) else {
            continue;
        };
        let why: Vec<&str> = fields.collect();
        match label {
            "listed" | "deployment-pending" | "would-close" if items.iter().any(|i| i == key) => {
                return Applied::Kept(format!("{key} {label} {}", why.join(" ")));
            }
            "scan-error" => return Applied::Kept(format!("scan-error {key} {}", why.join(" "))),
            _ => {}
        }
    }
    Applied::Done
}

/// `pg-spawnd writer-stop` exits 0 when it keeps a writer; the report row says which.
pub(super) fn parse_stop_report(raw: &str) -> Result<Applied, String> {
    let rows: Value = serde_json::from_str(raw).map_err(|_| "invalid writer stop report")?;
    let [row] = rows.as_array().map(Vec::as_slice).unwrap_or(&[]) else {
        return Err("writer stop report needs exactly one row".into());
    };
    match row["status"].as_str() {
        Some("stopped") => Ok(Applied::Done),
        Some("kept") => Ok(Applied::Kept(
            row["reason"]
                .as_str()
                .unwrap_or("no reason given")
                .to_owned(),
        )),
        _ => Err("writer stop report row has no status".into()),
    }
}

pub(super) fn parse_hosts(raw: &str) -> Result<Vec<HostReceipt>, String> {
    let v: Value = serde_json::from_str(raw).map_err(|_| "invalid verify-hosts output")?;
    v["verified"]
        .as_array()
        .ok_or("verify-hosts: no receipts")?
        .iter()
        .map(|r| {
            Ok(HostReceipt {
                host: r["host"].as_str().ok_or("receipt without host")?.to_owned(),
                head: r["head"].as_str().ok_or("receipt without head")?.to_owned(),
            })
        })
        .collect()
}

struct Real {
    repo: String,
    dir: PathBuf,
    /// The keeper closes by merge evidence for every merged item in one pass; its report is read per part.
    keeper_report: Option<String>,
}

impl Effects for Real {
    fn pr_head(&mut self, pr: u64) -> Result<(PrState, String), String> {
        let raw = output(
            Command::new("gh").args([
                "pr",
                "view",
                &pr.to_string(),
                "--repo",
                &self.repo,
                "--json",
                "state,headRefOid",
            ]),
            "gh pr view",
        )?;
        parse_pr_view(&raw)
    }

    fn in_landed_tree(&mut self, sha: &str, merge: &str) -> Result<bool, String> {
        let status = Command::new("git")
            .arg("-C")
            .arg(&self.dir)
            .args(["merge-base", "--is-ancestor", sha, merge])
            .status()
            .map_err(|e| format!("git: {e}"))?;
        match status.code() {
            Some(0) => Ok(true),
            Some(1) => Ok(false),
            _ => Err(format!(
                "git merge-base could not compare {sha} with {merge}"
            )),
        }
    }

    fn close_items(&mut self, part: &PartState, _proof: &Proof) -> Result<Applied, String> {
        // The keeper proves a folded part through the train merge itself; one pass covers every part.
        if self.keeper_report.is_none() {
            let exe = std::env::current_exe().map_err(|e| e.to_string())?;
            self.keeper_report = Some(output(Command::new(exe).arg("merge-close"), "merge-close")?);
        }
        Ok(parse_close_report(
            self.keeper_report.as_deref().unwrap_or_default(),
            &part.items,
        ))
    }

    fn verify_hosts(&mut self, merge: &str) -> Result<Vec<HostReceipt>, String> {
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let raw = output(
            Command::new(exe).args(["landing", "verify-hosts", &self.repo, merge]),
            "verify-hosts",
        )?;
        parse_hosts(&raw)
    }

    fn close_pr(&mut self, pr: u64) -> Result<(), String> {
        output(
            Command::new("gh").args(["pr", "close", &pr.to_string(), "--repo", &self.repo]),
            "gh pr close",
        )
        .map(drop)
    }

    fn reopen_pr(&mut self, pr: u64) -> Result<(), String> {
        output(
            Command::new("gh").args(["pr", "reopen", &pr.to_string(), "--repo", &self.repo]),
            "gh pr reopen",
        )
        .map(drop)
    }

    fn stop_writer(&mut self, part: &PartState, merge: &str) -> Result<Applied, String> {
        let Some(record) = &part.writer_record else {
            return Err(format!("part #{} has no writer record to stop", part.pr));
        };
        let raw = output(
            Command::new("pg-spawnd")
                .args(["writer-stop", merge])
                .arg(record),
            "pg-spawnd writer-stop",
        )?;
        parse_stop_report(&raw)
    }

    /// The hold of rule 3.64 is the `hold_released` field of the state file; nothing else to switch.
    fn end_hold(&mut self) -> Result<(), String> {
        Ok(())
    }
}

pub(super) fn run(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("usage: plangraph land-train settle --state FILE --repo-dir DIR [--init FILE]\nmissing --{k}"))
    };
    let state = PathBuf::from(need("state")?);
    let mut settlement = match Settlement::load(&state)? {
        Some(s) => s,
        None => {
            let init: Init = serde_json::from_slice(
                &std::fs::read(need("init")?).map_err(|e| format!("settle --init: {e}"))?,
            )
            .map_err(|e| format!("settle --init: {e}"))?;
            init.into_settlement()?
        }
    };
    let mut fx = Real {
        repo: settlement.repo.clone(),
        dir: PathBuf::from(need("repo-dir")?),
        keeper_report: None,
    };
    let now = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs() as i64)
    };
    let summary = settlement.run(&state, &mut fx, &now)?;
    let mut lines = vec![format!(
        "settle #{}: {} steps done, {} held, {}",
        settlement.train_pr,
        summary.done,
        summary.held.len(),
        if summary.complete { "complete" } else { "open" }
    )];
    lines.extend(summary.held.iter().map(|h| format!("held {h}")));
    Ok(lines)
}
