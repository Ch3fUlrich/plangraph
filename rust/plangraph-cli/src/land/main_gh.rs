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

/// The tier an attempt ran on: `fast` when it listed jobs but none of the full suite (a landed tip's own
/// run), otherwise the SQLite job count tells the self-hosted tier (four shards) from the fallback.
pub(super) fn tier_of(jobs: &[Job]) -> String {
    let full_only = |j: &Job| {
        j.name.starts_with("Python tests")
            || j.name.starts_with("Rust workspace")
            || j.name.starts_with("UI tests")
    };
    if !jobs.is_empty() && !jobs.iter().any(full_only) {
        return "fast".into();
    }
    let own = jobs
        .iter()
        .any(|j| j.name.starts_with("Python tests, SQLite keeper") && j.name.ends_with("of 4)"));
    if own { "self" } else { "hosted" }.into()
}

/// run id -> attempt -> (newest status time, succeeded): the attempts a `ci-full` status ties to the tip's tree.
type TestedTree = BTreeMap<u64, BTreeMap<u32, (String, bool)>>;

pub struct GhSource<G: Github> {
    pub gh: G,
    pub branch: String,
    pub now: fn() -> i64,
    /// The id of the identity whose `land-auth` commit status is trusted landing evidence; without it no
    /// run is ever treated as fast-only and the full suite is expected on every tip.
    pub land_app: Option<u64>,
}

/// The identity that publishes `ci-full` statuses (the Actions bot by default; `PLANGRAPH_CI_APP_ID` overrides).
pub fn ci_publisher() -> u64 {
    std::env::var("PLANGRAPH_CI_APP_ID")
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(41_898_282)
}

/// `PLANGRAPH_LAND_APP_ID`: the trusted creator of `land-auth` statuses.
pub fn land_app_from_env() -> Option<u64> {
    std::env::var("PLANGRAPH_LAND_APP_ID")
        .ok()?
        .trim()
        .parse()
        .ok()
}

impl<G: Github> GhSource<G> {
    /// The statuses of the commit, read once per pass.
    fn statuses(&mut self, tip: &str) -> Result<Vec<Value>, String> {
        self.gh
            .page(&format!("commits/{tip}/statuses?per_page=100"), None)
    }

    /// The evidence a trusted `land-auth` success names: the App writes the description
    /// `attempt <id> evidence run <run id> attempt <n>`; only the configured identity counts.
    fn land_evidence(&self, statuses: &[Value]) -> Option<(u64, u32)> {
        let app = self.land_app?;
        statuses
            .iter()
            .filter(|s| {
                s["context"] == "land-auth"
                    && s["state"] == "success"
                    && s["creator"]["id"].as_u64() == Some(app)
            })
            .find_map(|s| {
                let text = s["description"].as_str()?;
                let mut words = text.split_whitespace().skip_while(|w| *w != "evidence");
                let (_, kw, run, kw2, n) = (
                    words.next()?,
                    words.next()?,
                    words.next()?,
                    words.next()?,
                    words.next()?,
                );
                (kw == "run" && kw2 == "attempt").then_some((run.parse().ok()?, n.parse().ok()?))
            })
    }

    /// The runs and attempts whose `ci-full` status names the tree of the tip itself (`run <id> attempt <n>
    /// tree <sha>`): the only full attempts of other refs that may decide main's state. A pull request run
    /// reports the tip as its head but may have tested a merge ref of another tree.
    fn tested_tree(&mut self, tip: &str, statuses: &[Value]) -> Result<TestedTree, String> {
        let tree = self.gh.value(&format!("git/commits/{tip}"))?["tree"]["sha"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        let publisher = ci_publisher();
        let mut out: TestedTree = BTreeMap::new();
        // Only the CI publisher's statuses count; a later failure is kept for recovery, success is `true`.
        for s in statuses
            .iter()
            .filter(|s| s["context"] == "ci-full" && s["creator"]["id"].as_u64() == Some(publisher))
        {
            let text = s["description"].as_str().unwrap_or_default();
            let w: Vec<&str> = text.split_whitespace().collect();
            if let ["run", run, "attempt", n, "tree", sha] = w[..] {
                if sha == tree {
                    if let (Ok(run), Ok(n)) = (run.parse::<u64>(), n.parse::<u32>()) {
                        // The newest status of an attempt wins (the API lists newest first, `updated_at`
                        // decides when present): an older pending status never replaces a result.
                        let ts = s["updated_at"].as_str().unwrap_or("").to_owned();
                        let ok = s["state"] == "success";
                        match out.entry(run).or_default().entry(n) {
                            std::collections::btree_map::Entry::Vacant(v) => {
                                v.insert((ts, ok));
                            }
                            std::collections::btree_map::Entry::Occupied(mut o) => {
                                // Times decide only when both statuses carry one; otherwise the API's newest-first order stands.
                                if !ts.is_empty() && !o.get().0.is_empty() && ts > o.get().0 {
                                    o.insert((ts, ok));
                                }
                            }
                        }
                    }
                }
            }
        }
        Ok(out)
    }

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
            concluded: if info["status"] == "completed" {
                epoch(&info, "updated_at")
            } else {
                0
            },
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
        let mut cache: Option<(Option<(u64, u32)>, TestedTree)> = None;
        for row in rows {
            // Runs of any ref on this commit are read: a fast-only tip is judged by the latest full attempt
            // of any ref (the evidence run ran on the train's branch); the evaluation filters by ref.
            if row["head_sha"] != tip {
                continue;
            }
            let id = row["id"].as_u64().ok_or("run without id")?;
            // A pull request run tests a merge ref whatever its head branch is called (a fork can name its
            // branch `main`), so it decides only through a ci-full status naming the tip's tree. Every other
            // event tests its ref's head commit, which is the tip: such a run is read as soon as it exists.
            let is_pr = matches!(
                row["event"].as_str(),
                Some("pull_request" | "pull_request_target")
            );
            let own_branch = row["head_branch"] == self.branch.as_str();
            let latest = row["run_attempt"].as_u64().unwrap_or(1) as u32;
            let mut attempts = Vec::new();
            for n in 1..=latest {
                attempts.push(self.attempt(id, n)?);
            }
            let looks_fast = attempts.last().is_some_and(|a| a.tier == "fast");
            if (is_pr || looks_fast) && cache.is_none() {
                let statuses = self.statuses(tip)?;
                let tested = self.tested_tree(tip, &statuses)?;
                cache = Some((self.land_evidence(&statuses), tested));
            }
            let (evidence, tested) = cache.clone().unwrap_or_default();
            if is_pr {
                // A pull request run decides only through a ci-full status that names the tip's tree.
                let Some(listed) = tested.get(&id) else {
                    continue;
                };
                let newest = listed.keys().max().copied().unwrap_or(0);
                // Attempts without evidence older than the newest listed one are dropped; a newer one is
                // unverified: it keeps the state pending (and stalls at T) instead of vanishing.
                attempts.retain(|a| listed.contains_key(&a.number) || a.number > newest);
                for a in attempts
                    .iter_mut()
                    .filter(|a| !listed.contains_key(&a.number))
                {
                    a.status = "in_progress".into();
                    a.conclusion = None;
                    a.concluded = 0;
                }
            }
            // Fast-only needs the trusted landing evidence, not just a short job list: a partial rerun of a
            // full run lists few jobs too and must still be judged against the full matrix. Only the branch's
            // own push run can be it, the evidence attempt must be a full attempt of the tip's tree, and the
            // evidence run itself stays in the full generation however short its job list.
            let fast = looks_fast
                && own_branch
                && row["event"] == "push"
                && evidence.is_some_and(|(run, n)| {
                    run != id
                        && tested
                            .get(&run)
                            .and_then(|l| l.get(&n))
                            .is_some_and(|s| s.1)
                });
            if looks_fast && !fast {
                for a in attempts.iter_mut().filter(|a| a.tier == "fast") {
                    a.tier = "hosted".into();
                }
            }
            out.push(Run {
                id,
                event: row["event"].as_str().unwrap_or_default().to_owned(),
                attempts,
                refname: row["head_branch"].as_str().unwrap_or_default().to_owned(),
                fast,
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
    fn branch(&self) -> String {
        self.branch.clone()
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
    let matrix = match matrix_from_toml(MATRIX) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let (repo, ledger_path) = match repo_and_ledger(home, named_repo.as_deref(), &branch) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let answer = answer_for(repo, &branch, ledger_path, &matrix);
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

/// Reads main's state live with the first-observation record at `ledger_path`.
pub fn answer_for(repo: String, branch: &str, ledger_path: PathBuf, matrix: &Matrix) -> Answer {
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
        branch: branch.to_owned(),
        now: now_secs,
        land_app: land_app_from_env(),
    };
    observe(&mut source, &mut ledger, matrix)
}

/// The repository named by `--repo` or by the `origin` remote, and where its tip ledger lives.
pub fn repo_and_ledger(
    home: &Path,
    named: Option<&str>,
    branch: &str,
) -> Result<(String, PathBuf), String> {
    let repo = match named {
        Some(r) => r.to_owned(),
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
    Ok((repo, ledger))
}
