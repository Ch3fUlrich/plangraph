//! One repository-wide Plan-Item index, shared by ready and claim.
use crate::truth_process::run;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::Path};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Commit {
    pub sha: String,
    pub date: String,
    pub subject: String,
    pub keys: Vec<String>,
    #[serde(default)]
    pub prs: Vec<u64>,
}

#[derive(Default, Clone, Debug, Deserialize, Serialize)]
pub struct Work {
    pub commits: Vec<Commit>,
    pub prs: Vec<u64>,
    pub lanes: Vec<String>,
}

#[derive(Default, Debug, Deserialize, Serialize)]
pub struct RepositoryIndex {
    pub items: BTreeMap<String, Work>,
}

impl RepositoryIndex {
    pub fn add_merged(&mut self, commits: Vec<Commit>) {
        for commit in commits {
            for key in &commit.keys {
                self.items
                    .entry(key.clone())
                    .or_default()
                    .commits
                    .push(commit.clone());
            }
        }
    }
    pub fn add_pr(&mut self, pr: u64, commits: Vec<Commit>) {
        for commit in commits {
            for key in commit.keys {
                let work = self.items.entry(key).or_default();
                if !work.prs.contains(&pr) {
                    work.prs.push(pr);
                }
            }
        }
    }
    pub fn work(&self, key: &str) -> Work {
        let mut work = self.items.get(key).cloned().unwrap_or_default();
        if let Some((_, bare)) = key.split_once(':') {
            if let Some(other) = self.items.get(bare) {
                work.commits.extend(other.commits.clone());
                work.prs.extend(&other.prs);
            }
        }
        work.prs.sort_unstable();
        work.prs.dedup();
        work.commits
            .sort_by(|a, b| b.date.cmp(&a.date).then(a.sha.cmp(&b.sha)));
        work.commits.dedup_by(|a, b| a.sha == b.sha);
        work
    }
}

pub fn parse_commits(log: &str) -> Result<Vec<Commit>, String> {
    let mut commits = Vec::new();
    for record in log.split('\x1d') {
        let record = record.trim_matches('\n');
        if record.is_empty() {
            continue;
        }
        let fields: Vec<_> = record.split('\x1f').collect();
        if fields.len() != 4
            || fields[0].len() != 40
            || !fields[0].bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err("invalid repository log".into());
        }
        let keys = fields[3]
            .split('\x1e')
            .map(str::trim)
            .filter(|key| !key.is_empty())
            .map(str::to_owned)
            .collect();
        commits.push(Commit {
            prs: vec![],
            sha: fields[0].into(),
            date: fields[1].into(),
            subject: fields[2].into(),
            keys,
        });
    }
    Ok(commits)
}

pub fn history(repo: &Path, revision: &str) -> Result<Vec<Commit>, String> {
    crate::truth_graph::history(&run(
        repo,
        "git",
        &[
            "log",
            "--topo-order",
            "--no-show-signature",
            "--format=%H%x1f%cI%x1f%s%x1f%(trailers:key=Plan-Item,valueonly,separator=%x1e)%x1f%P%x1d",
            "--end-of-options",
            revision,
        ],
    )?)
}

pub fn collect(repo: &Path, reference: &str, refresh: bool) -> Result<RepositoryIndex, String> {
    let started = std::time::Instant::now();
    if reference.starts_with('-')
        || reference.contains("..")
        || !reference
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/_-.".contains(&b))
    {
        return Err("invalid default reference".into());
    }
    if refresh {
        run(repo, "git", &["fetch", "--prune", "origin"])?;
    }
    if run(repo, "git", &["rev-parse", "--is-shallow-repository"])?.trim() != "false" {
        return Err("repository history incomplete".into());
    }
    let remote_ref = format!("origin/{reference}");
    let reference = if refresh && !reference.contains('/') {
        &remote_ref
    } else {
        reference
    };
    let mut index = RepositoryIndex::default();
    index.add_merged(history(repo, reference)?);
    let prs: Vec<Pr> = serde_json::from_str(&run(
        repo,
        "gh",
        &[
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,headRefOid",
        ],
    )?)
    .map_err(|_| "invalid PR index")?;
    if prs.len() >= 1000 {
        return Err("PR index truncated".into());
    }
    for pr in prs {
        if started.elapsed().as_secs() > 180 {
            return Err("repository index refresh exceeded its time budget".into());
        }
        if pr.head_ref_oid.len() != 40 || !pr.head_ref_oid.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("invalid PR head".into());
        }
        if refresh {
            run(repo, "git", &["fetch", "origin", &pr.head_ref_oid])?;
        }
        index.add_pr(
            pr.number,
            history(repo, &format!("{reference}..{}", pr.head_ref_oid))?,
        );
    }
    Ok(index)
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Pr {
    number: u64,
    head_ref_oid: String,
}
