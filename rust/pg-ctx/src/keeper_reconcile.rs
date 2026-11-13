//! Offline proposals; only the existing router close verb writes keeper state.
use crate::keeper_truth::RepositoryIndex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Assessment {
    pub all_slices_merged: bool,
    pub done_criteria: String,
    pub commits: Vec<String>,
    pub prs: Vec<u64>,
    pub families: Vec<String>,
    pub reason: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Row {
    pub key: String,
    pub state: String,
    pub action: String,
    pub prs: Vec<u64>,
    pub commit_count: usize,
    pub last_commit_date: String,
    pub commits: Vec<String>,
    pub families: Vec<String>,
    pub note: String,
}

pub fn propose(
    items: &[Value],
    index: &RepositoryIndex,
    assessments: &BTreeMap<String, Assessment>,
) -> Result<Vec<Row>, String> {
    let projects: BTreeSet<_> = items
        .iter()
        .filter_map(|i| i["key"].as_str())
        .filter_map(|k| k.split_once(':').map(|p| p.0))
        .collect();
    if projects.len() > 1 {
        return Err("use one repository's snapshot per index".into());
    }
    let mut rows = Vec::new();
    let mut seen = BTreeSet::new();
    for item in items {
        let key = item["key"].as_str().ok_or("item key missing")?;
        if !seen.insert(key) {
            return Err("duplicate item key".into());
        }
        let state = item["state"].as_str().ok_or("item state missing")?;
        if !matches!(state, "open" | "claimed" | "in_review") {
            continue;
        }
        let work = index.work(key);
        if work.commits.is_empty() {
            continue;
        }
        let commits: Vec<_> = work.commits.iter().map(|c| c.sha.clone()).collect();
        let mut row = Row {
            key: key.into(),
            state: state.into(),
            action: "CHECK".into(),
            prs: work
                .commits
                .iter()
                .flat_map(|c| c.prs.iter().copied())
                .collect(),
            commit_count: commits.len(),
            last_commit_date: work.commits[0].date.clone(),
            commits,
            families: vec![],
            note: "Merged commits exist; done criteria require assessment".into(),
        };
        if let Some(a) = assessments.get(key) {
            let expected: BTreeSet<_> = row.commits.iter().collect();
            let supplied: BTreeSet<_> = a.commits.iter().collect();
            if expected != supplied
                || a.done_criteria.trim().is_empty()
                || Some(a.done_criteria.as_str()) != item["done_criteria"].as_str()
                || a.reason.trim().is_empty()
                || a.prs.is_empty()
                || a.prs.contains(&0)
            {
                row.note =
                    "Assessment stale or incomplete; recheck commits, PRs and done criteria".into();
            } else {
                row.prs = a.prs.clone();
                row.families = a.families.clone();
                row.action = if a.all_slices_merged {
                    "CLOSE"
                } else {
                    "SLICE-OPEN"
                }
                .into();
                row.note = format!(
                    "Merged PRs {:?}, commits {:?}; {}",
                    row.prs, row.commits, a.reason
                );
            }
        }
        rows.push(row);
    }
    rows.sort_by(|a, b| a.key.cmp(&b.key));
    Ok(rows)
}

pub fn apply_batch(
    rows: &[Row],
    decision: &str,
    mut close: impl FnMut(&str, &str, &[String]) -> Result<(), String>,
) -> Result<(), String> {
    if !decision
        .strip_prefix("D-")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err("batch requires a recorded D-id".into());
    }
    let closes: Vec<_> = rows.iter().filter(|r| r.action == "CLOSE").collect();
    let mut seen = BTreeSet::new();
    for row in &closes {
        if row.state != "in_review"
            || row.families.is_empty()
            || row.prs.is_empty()
            || row
                .families
                .iter()
                .any(|f| f.trim().is_empty() || f.starts_with('-'))
            || row.key.starts_with('-')
            || !seen.insert(&row.key)
        {
            return Err(
                "batch cannot close: restore review/seat evidence through the router first".into(),
            );
        }
    }
    for row in closes {
        close(&row.key, decision, &row.families)?;
    }
    Ok(())
}

pub fn tsv(rows: &[Row]) -> String {
    let mut output = "key\tstate\taction\tprs\tcommit_count\tlast_commit_date\tnote\n".to_owned();
    for row in rows {
        let clean = |s: &str| s.replace(['\t', '\n', '\r'], " ");
        output.push_str(&format!(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\n",
            clean(&row.key),
            row.state,
            row.action,
            row.prs
                .iter()
                .map(u64::to_string)
                .collect::<Vec<_>>()
                .join(","),
            row.commit_count,
            clean(&row.last_commit_date),
            clean(&row.note)
        ));
    }
    output
}

#[derive(Default, Debug, Deserialize, Serialize)]
pub struct AlertState {
    pub since: Option<u64>,
    pub alerted: bool,
}

impl AlertState {
    /// One alert per continuous nonzero episode, only after a full day.
    pub fn tick(&mut self, count: usize, now: u64) -> bool {
        if count == 0 {
            *self = Self::default();
            return false;
        }
        let since = *self.since.get_or_insert(now);
        if now.saturating_sub(since) > 86400 && !self.alerted {
            self.alerted = true;
            return true;
        }
        false
    }
}
