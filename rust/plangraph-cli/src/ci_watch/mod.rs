//! One bounded CI scan; all external effects are behind the Github adapter.
pub(crate) mod github;
mod keeper;
pub(crate) mod logs;
#[cfg(test)]
mod tests;

use crate::atomic;
use github::{Gh, Github};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

type Result<T> = std::result::Result<T, String>;
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Task {
    pub key: String,
    pub head: String,
    pub pr: Option<u64>,
    pub run_id: u64,
    pub job: String,
    pub test: String,
    pub excerpt: String,
    pub priority: u8,
    #[serde(default)]
    pub keeper_defined: bool,
    #[serde(default)]
    pub keeper_key: Option<String>,
}
#[derive(Deserialize)]
struct Flakes {
    flakes: BTreeMap<String, String>,
}
#[derive(Default, Deserialize, Serialize)]
struct Reruns {
    runs: BTreeSet<u64>,
    #[serde(default)]
    sightings: BTreeSet<String>,
    #[serde(default)]
    newest: BTreeMap<String, u64>,
}

fn read<T: serde::de::DeserializeOwned + Default>(path: &Path) -> Result<T> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| "invalid ci-watch state".into()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(_) => Err("cannot read ci-watch state".into()),
    }
}
fn save(path: &Path, value: &impl Serialize) -> Result<()> {
    atomic::write(path, &serde_json::to_vec(value).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}
fn save_tasks(path: &Path, tasks: &BTreeMap<String, Task>) -> Result<()> {
    let mut bytes = Vec::new();
    for task in tasks.values() {
        serde_json::to_writer(&mut bytes, task).map_err(|e| e.to_string())?;
        bytes.push(b'\n');
    }
    atomic::write(path, &bytes).map_err(|e| e.to_string())
}
fn number(value: &Value, field: &str) -> Result<u64> {
    value[field]
        .as_u64()
        .ok_or_else(|| format!("missing GitHub {field}"))
}
fn text<'a>(value: &'a Value, field: &str) -> Result<&'a str> {
    value[field]
        .as_str()
        .ok_or_else(|| format!("missing GitHub {field}"))
}

fn failed(value: &Value) -> bool {
    matches!(
        value["conclusion"].as_str(),
        Some("failure" | "timed_out" | "startup_failure")
    )
}

pub fn run(home: &Path, env: &BTreeMap<String, String>, args: &[String]) -> Result<()> {
    if args
        .iter()
        .any(|a| !matches!(a.as_str(), "--dry-run" | "--keeper"))
    {
        return Err("usage: plangraph ci-watch [--dry-run] [--keeper]".into());
    }
    let dry = args.iter().any(|a| a == "--dry-run");
    let use_keeper = args.iter().any(|a| a == "--keeper");
    let remote = github::command("git", &["remote", "get-url", "origin"])?;
    let repo = github::repository(&remote)?;
    let root = github::command("git", &["rev-parse", "--show-toplevel"])?;
    let mapping = fs::read_to_string(Path::new(root.trim()).join("deploy/ci-watch/flakes.toml"))
        .map_err(|_| "cannot read deploy/ci-watch/flakes.toml")?;
    let flakes: Flakes = toml::from_str(&mapping).map_err(|_| "invalid flake mapping")?;
    let dir = home.join(".local/state/plangraph/ci-watch");
    let rows = scan(
        &mut Gh { repo: repo.clone() },
        &repo,
        &dir,
        &flakes.flakes,
        dry,
        |task| {
            if use_keeper {
                keeper::define(env, task)
            } else {
                Ok(false)
            }
        },
    )?;
    let pending = rows
        .iter()
        .any(|row| row.starts_with("keeper delivery pending:"));
    for row in rows {
        println!("{row}");
    }
    if pending {
        return Err("keeper delivery incomplete; local tasks retained".into());
    }
    Ok(())
}

fn scan(
    gh: &mut impl Github,
    repo: &str,
    dir: &Path,
    flakes: &BTreeMap<String, String>,
    dry: bool,
    mut define: impl FnMut(&mut Task) -> Result<bool>,
) -> Result<Vec<String>> {
    // Dry runs do not create even a lock file or directory.
    let _lock = if dry {
        None
    } else {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        Some(atomic::lock(&dir.join("scan")).map_err(|e| e.to_string())?)
    };
    let task_path = dir.join("tasks.jsonl");
    let mut tasks = BTreeMap::<String, Task>::new();
    match fs::read_to_string(&task_path) {
        Ok(raw) => {
            for line in raw.lines() {
                let task: Task = serde_json::from_str(line).map_err(|_| "invalid task state")?;
                tasks.insert(task.key.clone(), task);
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(_) => return Err("cannot read tasks".into()),
    }
    let mut reruns: Reruns = read(&dir.join("reruns.json"))?;
    let prs = gh.pages("pulls?state=open&per_page=100", None)?;
    let mut heads = BTreeMap::new();
    for pr in prs {
        let sha = text(&pr["head"], "sha")?.to_owned();
        heads.insert(sha, number(&pr, "number")?);
    }
    let workflow = "actions/workflows/tests.yml/runs?status=completed&per_page=100";
    // Only the newest completed run per head/event is used and GitHub lists
    // newest first, so one page per query bounds API use regardless of history.
    let mut runs = gh.page(&format!("{workflow}&branch=main"), Some("workflow_runs"))?;
    for sha in heads.keys() {
        runs.extend(gh.page(&format!("{workflow}&head_sha={sha}"), Some("workflow_runs"))?);
    }
    // GitHub returns newest first, but sorting also makes fixtures/order changes safe.
    runs.sort_by_key(|r| std::cmp::Reverse(r["id"].as_u64().unwrap_or(0)));
    let mut seen = BTreeSet::new();
    let mut output = Vec::new();
    for run in runs {
        let sha = text(&run, "head_sha")?;
        let pr = heads.get(sha).copied();
        let event = text(&run, "event")?;
        let head = if let Some(pr) = pr {
            format!("pr-{pr}")
        } else if run["head_branch"] == "main"
            && event != "pull_request"
            && event != "pull_request_target"
        {
            "main".into()
        } else {
            continue;
        };
        let run_id = number(&run, "id")?;
        let selection = format!("{repo}/{head}/{event}");
        // Our rerun temporarily disappears from the completed-only response.
        // Never walk backwards into older failures while that attempt runs.
        if reruns.newest.get(&selection).is_some_and(|id| *id > run_id)
            || !seen.insert((head.clone(), event.to_owned()))
        {
            continue;
        }
        if !dry && reruns.newest.get(&selection) != Some(&run_id) {
            reruns.newest.insert(selection, run_id);
            save(&dir.join("reruns.json"), &reruns)?;
        }
        if !failed(&run) {
            continue;
        }
        let jobs = gh
            .pages(
                &format!("actions/runs/{run_id}/jobs?filter=latest&per_page=100"),
                Some("jobs"),
            )
            .unwrap_or_default();
        let mut failures = Vec::new();
        for job in jobs {
            if !failed(&job) {
                continue;
            }
            let name = logs::scrub(text(&job, "name")?);
            let log = gh
                .log(number(&job, "id")?)
                .unwrap_or_else(|_| "Job log unavailable; inspect the failed job on GitHub".into());
            for (test, excerpt) in logs::failures(&log) {
                failures.push((name.clone(), logs::scrub(&test), excerpt));
            }
        }
        if failures.is_empty() {
            failures.push((
                "<workflow>".into(),
                "<job-failure>".into(),
                "No failing job logs available".into(),
            ));
        }
        let known: BTreeSet<_> = failures
            .iter()
            .filter_map(|(_, test, _)| flakes.get(test).cloned())
            .collect();
        let attempted =
            run["run_attempt"].as_u64().unwrap_or(1) > 1 || reruns.runs.contains(&run_id);
        let mut rerun_failed = false;
        if !known.is_empty() {
            let action = if attempted {
                "recurrence"
            } else {
                "rerun failed jobs"
            };
            let sighting = format!(
                "{run_id}:{}:{action}",
                run["run_attempt"].as_u64().unwrap_or(1)
            );
            let new_sighting = !reruns.sightings.contains(&sighting);
            if new_sighting {
                output.push(format!(
                    "{action}: {head} run {run_id} {}",
                    known.iter().cloned().collect::<Vec<_>>().join(", ")
                ));
            }
            if !dry {
                // Reserve before requesting: a lost response must not cause another rerun.
                if !attempted {
                    reruns.runs.insert(run_id);
                    save(&dir.join("reruns.json"), &reruns)?;
                    if gh.rerun(run_id).is_err() {
                        rerun_failed = true;
                        output.push(format!(
                            "rerun request failed: {head} run {run_id}; creating fix tasks"
                        ));
                    }
                }
                if new_sighting {
                    let line = serde_json::json!({"run_id":run_id,"head":head,"action":action,"keepers":known,"at":chrono::Utc::now().to_rfc3339()});
                    let path = dir.join("recurrences.jsonl");
                    let mut bytes = match fs::read(&path) {
                        Ok(b) => b,
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
                        Err(e) => return Err(e.to_string()),
                    };
                    bytes.extend(serde_json::to_vec(&line).map_err(|e| e.to_string())?);
                    bytes.push(b'\n');
                    atomic::write(&path, &bytes).map_err(|e| e.to_string())?;
                    reruns.sightings.insert(sighting);
                    save(&dir.join("reruns.json"), &reruns)?;
                }
            }
        }
        for (job, test, excerpt) in failures {
            if !attempted && !rerun_failed && flakes.contains_key(&test) {
                continue;
            }
            let digest = Sha256::digest(format!("{repo}\0{head}\0{test}").as_bytes());
            let key = format!("plangraph:CI-FIX-{digest:x}");
            let old_defined = tasks
                .get(&key)
                .is_some_and(|t| t.keeper_defined && t.run_id == run_id);
            let keeper_key = tasks.get(&key).and_then(|t| t.keeper_key.clone());
            let mut task = Task {
                key: key.clone(),
                head: head.clone(),
                pr,
                run_id,
                job,
                test,
                excerpt,
                priority: 10,
                keeper_defined: old_defined,
                keeper_key,
            };
            output.push(format!(
                "{}task: {}",
                if dry { "would upsert " } else { "" },
                serde_json::to_string(&task).map_err(|e| e.to_string())?
            ));
            if !dry {
                // Persist first so a keeper outage never loses a fix task.
                tasks.insert(key.clone(), task.clone());
                save_tasks(&task_path, &tasks)?;
                if !task.keeper_defined {
                    match define(&mut task) {
                        Ok(defined) => task.keeper_defined = defined,
                        Err(error) => output.push(format!(
                            "keeper delivery pending: {}: {}",
                            task.key,
                            logs::scrub(&error)
                        )),
                    }
                }
                tasks.insert(key, task);
                save_tasks(&task_path, &tasks)?;
            }
        }
    }
    Ok(output)
}
