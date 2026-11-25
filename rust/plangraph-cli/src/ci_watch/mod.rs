//! One bounded CI scan; all external effects are behind the Github adapter.
#[cfg(test)]
mod batch_delivery_tests;
pub mod batch_failure;
#[cfg(test)]
mod batch_failure_tests;
pub(crate) mod github;
mod keeper;
#[cfg(test)]
mod ledger_tests;
pub(crate) mod logs;
pub mod main_freeze;
pub mod main_freeze_gh;
#[cfg(test)]
mod main_freeze_gh_tests;
#[cfg(test)]
mod main_freeze_tests;
mod sweep;
#[cfg(test)]
mod sweep_match_tests;
#[cfg(test)]
mod sweep_tests;
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
    /// The head commit of the failing run; the sweep cancels the item once the pull request moves on.
    #[serde(default)]
    pub sha: Option<String>,
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
fn load_tasks(path: &Path) -> Result<BTreeMap<String, Task>> {
    let mut tasks = BTreeMap::<String, Task>::new();
    match fs::read_to_string(path) {
        Ok(raw) => {
            for line in raw.lines() {
                let task: Task = serde_json::from_str(line).map_err(|_| "invalid task state")?;
                tasks.insert(task.key.clone(), task);
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(_) => return Err("cannot read tasks".into()),
    }
    Ok(tasks)
}
/// Read-modify-write of the task ledger under its own lock. The scan, the sweep, the main-freeze handler
/// and the keeper checkpoint each hold a different pass lock, so every change to `tasks.jsonl` goes through
/// here and none writes back a copy it read before another changed the file.
fn update_tasks<T>(
    path: &Path,
    change: impl FnOnce(&mut BTreeMap<String, Task>) -> T,
) -> Result<T> {
    let _lock = atomic::lock(path).map_err(|e| e.to_string())?;
    let mut tasks = load_tasks(path)?;
    let out = change(&mut tasks);
    save_tasks(path, &tasks)?;
    Ok(out)
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
    if args.iter().any(|a| {
        !matches!(a.as_str(), "--dry-run" | "--keeper" | "--main-freeze")
            && !a.starts_with("--ack=")
    }) {
        return Err(
            "usage: plangraph ci-watch [--dry-run] [--keeper] [--main-freeze] [--ack=ID]".into(),
        );
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
    let keeper_define = |task: &mut Task| {
        if use_keeper {
            keeper::define(env, task, &dir.join("tasks.jsonl"))
        } else {
            Ok(false)
        }
    };
    if let Some(id) = args.iter().find_map(|a| a.strip_prefix("--ack=")) {
        return main_freeze_gh::update_failures(&dir, |records| {
            if batch_failure::acknowledge(records, id) {
                Ok(())
            } else {
                Err("unknown failure record".into())
            }
        });
    }
    let now = chrono::Utc::now().timestamp();
    let mut rows = scan_and_sweep(
        &mut Gh { repo: repo.clone() },
        &repo,
        &dir,
        &flakes.flakes,
        dry,
        |task| {
            // A failing head of a batch with a recorded owner goes to that batch's writer, not to a fix lane.
            if dry {
                return keeper_define(task);
            }
            main_freeze_gh::route_or_define(&dir, task, now, keeper_define)
        },
        |task| main_freeze_gh::route_owned(&dir, task, now),
        |key: &str, reason: &str| cancel_item(use_keeper, env, key, reason),
    )?;
    if args.iter().any(|a| a == "--main-freeze") && !dry {
        rows.extend(run_main_freeze(
            &repo,
            &dir,
            &flakes.flakes,
            now,
            keeper_define,
            |key: &str| {
                if use_keeper {
                    keeper::cancel(env, key)
                } else {
                    Ok(())
                }
            },
        )?);
    }
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

/// Cancels a keeper item. Without `--keeper` nothing is closed there, so the call fails and the sweep
/// keeps its local row until a run with the keeper can cancel the item.
fn cancel_item(
    use_keeper: bool,
    env: &BTreeMap<String, String>,
    key: &str,
    reason: &str,
) -> Result<()> {
    if use_keeper {
        keeper::cancel_for(env, key, reason)
    } else {
        Err("run with --keeper to cancel the keeper item".into())
    }
}

/// An open pull request's head commit, and what a run needs to be matched to it.
struct Head {
    pr: u64,
    owner_only: bool,
    branch: String,
    /// The repository the head branch lives in (differs from the base for a fork).
    source: Option<String>,
}

/// The pull requests (or main) a run belongs to. A push to main is main's alone, whatever pull requests
/// share its commit. Any other run belongs to the pull requests it names (`pull_requests`); a run that
/// names none belongs to the one pull request on its commit whose head branch and head repository it ran
/// on (both repositories known and equal), and to none when that is not exactly one (two forks can share a
/// branch name and a commit).
fn targets_of(
    run: &Value,
    sha: &str,
    event: &str,
    heads: &BTreeMap<String, Vec<Head>>,
) -> Vec<(Option<u64>, bool)> {
    if run["head_branch"] == "main" && event != "pull_request" && event != "pull_request_target" {
        return vec![(None, false)];
    }
    let on_commit: &[Head] = heads.get(sha).map_or(&[], Vec::as_slice);
    let named: Vec<u64> = run["pull_requests"]
        .as_array()
        .map(|prs| prs.iter().filter_map(|pr| pr["number"].as_u64()).collect())
        .unwrap_or_default();
    let chosen: Vec<&Head> = if !named.is_empty() {
        on_commit
            .iter()
            .filter(|head| named.contains(&head.pr))
            .collect()
    } else {
        let ran_in = run["head_repository"]["full_name"].as_str();
        let same: Vec<&Head> = on_commit
            .iter()
            .filter(|head| {
                run["head_branch"].as_str() == Some(head.branch.as_str())
                    // Both repositories must be known and equal: a run or a pull request that does not
                    // say where its branch lives cannot be matched to a branch name alone.
                    && matches!((ran_in, head.source.as_deref()), (Some(ran), Some(source)) if ran == source)
            })
            .collect();
        if same.len() == 1 {
            same
        } else {
            Vec::new()
        }
    };
    chosen
        .into_iter()
        .map(|head| (Some(head.pr), head.owner_only))
        .collect()
}

/// One pass: the sweep first, so an item of an old head is cancelled before the scan files the same
/// failing test on the moved head, then the scan.
#[allow(clippy::too_many_arguments)]
fn scan_and_sweep(
    gh: &mut impl Github,
    repo: &str,
    dir: &Path,
    flakes: &BTreeMap<String, String>,
    dry: bool,
    define: impl FnMut(&mut Task) -> Result<bool>,
    owner: impl FnMut(&mut Task) -> Result<bool>,
    cancel: impl FnMut(&str, &str) -> Result<()>,
) -> Result<Vec<String>> {
    let mut rows = sweep::sweep(gh, dir, dry, cancel)?;
    rows.extend(scan_with_owner(gh, repo, dir, flakes, dry, define, owner)?);
    Ok(rows)
}

/// The scan of a repository without landing trains' owner delivery.
#[cfg(test)]
fn scan(
    gh: &mut impl Github,
    repo: &str,
    dir: &Path,
    flakes: &BTreeMap<String, String>,
    dry: bool,
    define: impl FnMut(&mut Task) -> Result<bool>,
) -> Result<Vec<String>> {
    scan_with_owner(gh, repo, dir, flakes, dry, define, |_| Ok(false))
}

/// `owner` hears the failures of landing trains (true when a recorded batch took it); `define` files
/// the fix items of every other live pull request and of main.
fn scan_with_owner(
    gh: &mut impl Github,
    repo: &str,
    dir: &Path,
    flakes: &BTreeMap<String, String>,
    dry: bool,
    mut define: impl FnMut(&mut Task) -> Result<bool>,
    mut owner: impl FnMut(&mut Task) -> Result<bool>,
) -> Result<Vec<String>> {
    // Dry runs do not create even a lock file or directory.
    let _lock = if dry {
        None
    } else {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        Some(atomic::lock(&dir.join("scan")).map_err(|e| e.to_string())?)
    };
    let task_path = dir.join("tasks.jsonl");
    let mut tasks = load_tasks(&task_path)?;
    let mut reruns: Reruns = read(&dir.join("reruns.json"))?;
    let prs = gh.pages("pulls?state=open&per_page=100", None)?;
    let mut heads = BTreeMap::new();
    for pr in prs {
        // A draft and a CI probe belong to their own writer; a landing train reaches only its recorded
        // owner. Neither gets a fix lane.
        let lane = sweep::lane(&pr);
        if lane == sweep::Lane::None {
            continue;
        }
        let sha = text(&pr["head"], "sha")?.to_owned();
        // One commit can head several pull requests (a train and an ordinary one): each keeps its entry.
        let branch = pr["head"]["ref"].as_str().unwrap_or_default().to_owned();
        let source = pr["head"]["repo"]["full_name"].as_str().map(str::to_owned);
        heads.entry(sha).or_insert_with(Vec::new).push(Head {
            pr: number(&pr, "number")?,
            owner_only: lane == sweep::Lane::Owner,
            branch,
            source,
        });
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
        let event = text(&run, "event")?;
        let targets = targets_of(&run, sha, event, &heads);
        for (pr, owner_only) in targets {
            let head = match pr {
                Some(pr) => format!("pr-{pr}"),
                None => "main".to_owned(),
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
                let log = gh.log(number(&job, "id")?).unwrap_or_else(|_| {
                    "Job log unavailable; inspect the failed job on GitHub".into()
                });
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
                if owner_only {
                    let mut task = Task {
                        key: key.clone(),
                        head: head.clone(),
                        pr,
                        run_id,
                        job,
                        test,
                        excerpt,
                        priority: 10,
                        keeper_defined: false,
                        keeper_key: None,
                        sha: Some(sha.to_owned()),
                    };
                    if dry {
                        output.push(format!("would route train failure: {key}"));
                    } else {
                        match owner(&mut task) {
                            Ok(true) => {
                                output.push(format!("routed train failure to its batch: {key}"))
                            }
                            Ok(false) => {
                                output.push(format!("train failure has no recorded batch: {key}"))
                            }
                            Err(error) => output.push(format!(
                                "keeper delivery pending: {key}: {}",
                                logs::scrub(&error)
                            )),
                        }
                    }
                    continue;
                }
                // The sweep could not cancel the item of an older head (keeper down): keep that row, and
                // its keeper lane, for the next pass instead of reusing the lane for the new head. A row
                // that never stored its head counts as moved when it was filed by another run.
                if let Some(old) = tasks.get(&key) {
                    let moved = match &old.sha {
                        Some(then) => then != sha,
                        None => old.run_id != run_id,
                    };
                    if !dry && pr.is_some() && moved {
                        output.push(format!("cancel pending: {key}"));
                        continue;
                    }
                }
                let old_defined = tasks
                    .get(&key)
                    .is_some_and(|t| t.keeper_defined && t.run_id == run_id);
                let keeper_key = tasks.get(&key).and_then(|t| t.keeper_key.clone());
                let task = Task {
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
                    sha: Some(sha.to_owned()),
                };
                output.push(format!(
                    "{}task: {}",
                    if dry { "would upsert " } else { "" },
                    serde_json::to_string(&task).map_err(|e| e.to_string())?
                ));
                if !dry {
                    store_task(&task_path, &mut tasks, task, &mut define, &mut output)?;
                }
            }
        }
    }
    Ok(output)
}

/// Persists a fix task first, so a keeper outage never loses it, then defines it in the keeper.
fn store_task(
    path: &Path,
    tasks: &mut BTreeMap<String, Task>,
    mut task: Task,
    define: &mut impl FnMut(&mut Task) -> Result<bool>,
    output: &mut Vec<String>,
) -> Result<bool> {
    let key = task.key.clone();
    tasks.insert(key.clone(), task.clone());
    update_tasks(path, |rows| rows.insert(key.clone(), task.clone()))?;
    let mut pending = false;
    if !task.keeper_defined {
        match define(&mut task) {
            Ok(defined) => task.keeper_defined = defined,
            Err(error) => {
                pending = true;
                output.push(format!(
                    "keeper delivery pending: {}: {}",
                    task.key,
                    logs::scrub(&error)
                ))
            }
        }
    }
    tasks.insert(key.clone(), task.clone());
    update_tasks(path, |rows| rows.insert(key, task))?;
    Ok(pending)
}

fn run_main_freeze(
    repo: &str,
    dir: &Path,
    flakes: &BTreeMap<String, String>,
    now: i64,
    define: impl FnMut(&mut Task) -> Result<bool>,
    close: impl FnMut(&str) -> Result<()>,
) -> Result<Vec<String>> {
    use crate::land::main_gh::{matrix_from_toml, FileLedger, GhSource, MATRIX};
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let compare = repo.to_owned();
    let mut ledger = FileLedger {
        path: dir.join("main-tips.json"),
        descends: move |old: &str, new: &str| {
            Gh {
                repo: compare.clone(),
            }
            .value(&format!("compare/{old}...{new}"))
            .map(|v| matches!(v["status"].as_str(), Some("ahead" | "identical")))
            .unwrap_or(false)
        },
    };
    let mut source = GhSource {
        gh: Gh {
            repo: repo.to_owned(),
        },
        branch: "main".into(),
        now: || chrono::Utc::now().timestamp(),
        land_app: crate::land::main_gh::land_app_from_env(),
    };
    let answer =
        crate::land::main_state::observe(&mut source, &mut ledger, &matrix_from_toml(MATRIX)?);
    let mut gh = Gh {
        repo: repo.to_owned(),
    };
    let mut fx = main_freeze_gh::GhEffects {
        gh: &mut gh,
        repo: repo.to_owned(),
        dir,
        define,
        close,
    };
    main_freeze_gh::pass(dir, &answer, flakes, &mut fx, now)
}
