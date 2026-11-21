//! GitHub and file adapters of the main-state function, and the `plangraph main-state` command.
use super::main_state::{admit, observe, Answer, Attempt, Job, Matrix, Run, Source, TipLedger};
use crate::atomic;
use crate::ci_watch::github::{self, Gh, Github};
use chrono::DateTime;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// The expected jobs per tier; one file for the watcher, the gates and the tests.
pub const MATRIX: &str = include_str!("../../../../deploy/ci-watch/matrix.toml");
const WORKFLOW: &str = "actions/workflows/tests.yml/runs";

/// The matrix file `deploy/ci-watch/matrix.toml`: tier name -> job display name -> suite.
pub fn matrix_from_toml(text: &str) -> Result<Matrix, String> {
    let tiers: BTreeMap<String, BTreeMap<String, String>> =
        toml::from_str(text).map_err(|e| format!("invalid job matrix: {e}"))?;
    Ok(Matrix { tiers })
}

fn epoch(value: &Value, field: &str) -> i64 {
    value[field]
        .as_str()
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .map_or(0, |t| t.timestamp())
}

/// The tier an attempt ran on: the SQLite job count tells the self-hosted tier (four shards) from the fallback.
fn tier_of(jobs: &[Job]) -> String {
    let own = jobs
        .iter()
        .any(|j| j.name.starts_with("Python tests, SQLite keeper") && j.name.ends_with("of 4)"));
    if own { "self" } else { "hosted" }.into()
}

pub struct GhSource<G: Github> {
    pub gh: G,
    pub branch: String,
    pub now: fn() -> i64,
}

impl<G: Github> GhSource<G> {
    fn attempt(&mut self, run: u64, number: u32) -> Result<Attempt, String> {
        let info = self
            .gh
            .value(&format!("actions/runs/{run}/attempts/{number}"))?;
        let rows = self.gh.pages(
            &format!("actions/runs/{run}/attempts/{number}/jobs?per_page=100"),
            Some("jobs"),
        )?;
        let jobs: Vec<Job> = rows
            .iter()
            .map(|j| Job {
                name: j["name"].as_str().unwrap_or_default().to_owned(),
                conclusion: j["conclusion"].as_str().map(str::to_owned),
                partial_log: false,
                tests: vec![],
            })
            .collect();
        Ok(Attempt {
            number,
            status: info["status"].as_str().unwrap_or("queued").to_owned(),
            conclusion: info["conclusion"].as_str().map(str::to_owned),
            started: epoch(&info, "run_started_at").max(epoch(&info, "created_at")),
            tier: tier_of(&jobs),
            jobs,
        })
    }
}

impl<G: Github> Source for GhSource<G> {
    fn forward_only(&mut self) -> Result<bool, String> {
        let branch = self.branch.clone();
        if let Ok(p) = self.gh.value(&format!("branches/{branch}/protection")) {
            let off = |k: &str| p[k]["enabled"].as_bool() == Some(false);
            return Ok(off("allow_force_pushes") && off("allow_deletions"));
        }
        let rules = self.gh.page(&format!("rules/branches/{branch}"), None)?;
        let has = |t: &str| rules.iter().any(|r| r["type"] == t);
        Ok(has("non_fast_forward") && has("deletion"))
    }
    fn tip(&mut self) -> Result<String, String> {
        let v = self.gh.value(&format!("git/ref/heads/{}", self.branch))?;
        v["object"]["sha"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| "ref has no sha".into())
    }
    fn runs(&mut self, tip: &str) -> Result<Vec<Run>, String> {
        let rows = self.gh.page(
            &format!("{WORKFLOW}?head_sha={tip}&per_page=100"),
            Some("workflow_runs"),
        )?;
        let mut out = Vec::new();
        for row in rows {
            if row["head_branch"] != self.branch.as_str() || row["head_sha"] != tip {
                continue;
            }
            let id = row["id"].as_u64().ok_or("run without id")?;
            let latest = row["run_attempt"].as_u64().unwrap_or(1) as u32;
            let mut attempts = Vec::new();
            for n in 1..=latest {
                attempts.push(self.attempt(id, n)?);
            }
            out.push(Run {
                id,
                event: row["event"].as_str().unwrap_or_default().to_owned(),
                attempts,
            });
        }
        Ok(out)
    }
    fn green_push_durations(&mut self) -> Result<Vec<i64>, String> {
        let rows = self.gh.page(
            &format!(
                "{WORKFLOW}?branch={}&event=push&status=success&per_page=20",
                self.branch
            ),
            Some("workflow_runs"),
        )?;
        Ok(rows
            .iter()
            .map(|r| (epoch(r, "updated_at") - epoch(r, "run_started_at")).max(0))
            .collect())
    }
    fn now(&mut self) -> i64 {
        (self.now)()
    }
}

#[derive(Default, Serialize, Deserialize)]
struct LedgerFile {
    last: Option<String>,
    seen: BTreeMap<String, i64>,
}

/// The first-observation record of a ref's tips, one file per repository and ref. It refuses a stale call
/// (the caller's last-known tip is not the recorded one) and a tip that does not descend from the recorded one.
pub struct FileLedger<F: FnMut(&str, &str) -> bool> {
    pub path: PathBuf,
    pub descends: F,
}

impl<F: FnMut(&str, &str) -> bool> FileLedger<F> {
    fn load(&self) -> LedgerFile {
        std::fs::read(&self.path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }
}

impl<F: FnMut(&str, &str) -> bool> TipLedger for FileLedger<F> {
    fn observe(&mut self, tip: &str, known_last: Option<&str>, now: i64) -> Result<i64, String> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        let _lock = atomic::lock(&self.path.with_extension("lock")).map_err(|e| e.to_string())?;
        let mut file = self.load();
        if let Some(first) = file.seen.get(tip) {
            return Ok(*first);
        }
        if file.last.as_deref() != known_last {
            return Err("stale: another reader recorded a newer tip".into());
        }
        if let Some(last) = file.last.clone() {
            if !(self.descends)(&last, tip) {
                return Err("tip does not descend from the recorded tip".into());
            }
        }
        file.seen.insert(tip.to_owned(), now);
        file.last = Some(tip.to_owned());
        atomic::write(
            &self.path,
            &serde_json::to_vec(&file).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        Ok(now)
    }
    fn last(&mut self) -> Option<String> {
        self.load().last
    }
}

fn now_secs() -> i64 {
    chrono::Utc::now().timestamp()
}

const USAGE: &str = "usage: plangraph main-state [--repo owner/name] [--branch main] [--admit] [--exception TOKEN]\n\
prints main's state with its evidence as JSON; with --admit exits 3 when a merge is refused";

/// `plangraph main-state`: exit 0 green (or admitted), 3 refused, 2 usage or setup error.
pub fn run(home: &Path, args: &[String]) -> i32 {
    let mut branch = "main".to_owned();
    let mut named_repo: Option<String> = None;
    let (mut admit_flag, mut exception) = (false, None);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--repo" => named_repo = it.next().cloned(),
            "--branch" => branch = it.next().cloned().unwrap_or_default(),
            "--admit" => admit_flag = true,
            "--exception" => exception = it.next().cloned(),
            _ => {
                eprintln!("{USAGE}");
                return 2;
            }
        }
    }
    let setup = || -> Result<(String, Matrix, PathBuf), String> {
        let repo = match &named_repo {
            Some(r) => r.clone(),
            None => github::repository(&github::command("git", &["remote", "get-url", "origin"])?)?,
        };
        if !repo
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-._/".contains(&b))
        {
            return Err("invalid repository".into());
        }
        let ledger = home
            .join(".local/state/plangraph/main-state")
            .join(format!("{}-{branch}.json", repo.replace('/', "_")));
        Ok((repo, matrix_from_toml(MATRIX)?, ledger))
    };
    let (repo, matrix, ledger_path) = match setup() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let compare_repo = repo.clone();
    let mut ledger = FileLedger {
        path: ledger_path,
        descends: move |old: &str, new: &str| {
            let mut gh = Gh {
                repo: compare_repo.clone(),
            };
            gh.value(&format!("compare/{old}...{new}"))
                .map(|v| matches!(v["status"].as_str(), Some("ahead" | "identical")))
                .unwrap_or(false)
        },
    };
    let mut source = GhSource {
        gh: Gh { repo },
        branch,
        now: now_secs,
    };
    let answer: Answer = observe(&mut source, &mut ledger, &matrix);
    println!("{}", serde_json::to_string(&answer).unwrap_or_default());
    if admit_flag {
        if let Err(word) = admit(&answer.state, exception.as_deref()) {
            eprintln!("{word}");
            return 3;
        }
        return 0;
    }
    i32::from(answer.state.refusal().is_some()) * 3
}
