//! One bounded CI scan; all external effects are behind the Github adapter.
pub mod autofix;
mod autofix_jail;
#[cfg(test)]
mod autofix_tests;
#[cfg(test)]
mod batch_delivery_tests;
pub mod batch_failure;
#[cfg(test)]
mod batch_failure_tests;
pub mod classify;
pub(crate) mod github;
mod keeper;
pub(crate) mod logs;
pub mod main_freeze;
pub mod main_freeze_gh;
#[cfg(test)]
mod main_freeze_gh_tests;
#[cfg(test)]
mod main_freeze_tests;
pub mod report;
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
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
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
    /// Class of the failing job (`classify::RedClass::name`) and of the whole run; empty on older rows.
    #[serde(default)]
    pub class: String,
    #[serde(default)]
    pub run_class: String,
    #[serde(default)]
    pub job_id: u64,
    /// Head commit of the run that failed; an automatic fix applies to it only.
    #[serde(default)]
    pub head_sha: String,
    /// Set by the newest scan on every failure it saw; a row a rerun made green stays false.
    #[serde(default)]
    pub current: bool,
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
        !matches!(
            a.as_str(),
            "--dry-run" | "--keeper" | "--main-freeze" | "--autofix" | "--report"
        ) && !a.starts_with("--ack=")
    }) {
        return Err(
            "usage: plangraph ci-watch [--dry-run] [--keeper] [--main-freeze] [--autofix] [--report] [--ack=ID]".into(),
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
            keeper::define(env, task)
        } else {
            Ok(false)
        }
    };
    if args.iter().any(|a| a == "--report") {
        println!("{}", report::report(&dir)?);
        return Ok(());
    }
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
    let rows = scan(
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
    )?;
    let mut rows = rows;
    if args.iter().any(|a| a == "--autofix") && !dry {
        let tasks = load_tasks(&dir.join("tasks.jsonl"))?;
        let mut gh = Gh { repo: repo.clone() };
        rows.extend(autofix::pass(
            &dir,
            &repo,
            &tasks,
            |pr| autofix::pr_head(&mut gh, &repo, pr),
            |sha| autofix::head_is_red(&mut Gh { repo: repo.clone() }, sha),
            |request, scratch| autofix::apply(scratch, request),
        )?);
    }
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
    let mut tasks = load_tasks(&task_path)?;
    // The newest scan marks what is red now; the rows of a head a rerun made green stop being current.
    tasks.values_mut().for_each(|task| task.current = false);
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
            let found = logs::failures(&log);
            // A known flake is classed per test: one flaky test never hides a real failure of the same job.
            let base = classify::classify_job(&name, &logs::scrub(&log), false);
            for (test, excerpt) in found {
                let class = if flakes.contains_key(&test) {
                    classify::RedClass::Flake
                } else {
                    base
                };
                failures.push((
                    name.clone(),
                    logs::scrub(&test),
                    excerpt,
                    class,
                    number(&job, "id")?,
                ));
            }
        }
        if failures.is_empty() {
            failures.push((
                "<workflow>".into(),
                "<job-failure>".into(),
                "No failing job logs available".into(),
                classify::RedClass::Unknown,
                0,
            ));
        }
        let known: BTreeSet<_> = failures
            .iter()
            .filter_map(|(_, test, ..)| flakes.get(test).cloned())
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
        let run_class = classify::classify_run(&failures.iter().map(|f| f.3).collect::<Vec<_>>());
        let jobs_seen: Vec<_> = failures
            .iter()
            .map(|f| (f.0.clone(), f.4, f.3.name().to_owned()))
            .collect();
        let mut made = 0usize;
        for (job, test, excerpt, class, job_id) in failures {
            if !attempted && !rerun_failed && flakes.contains_key(&test) {
                continue;
            }
            // A job that failed without a test id is told apart by its job id (names repeat across a matrix):
            // two such jobs are two failures.
            let identity = if test == "<job-failure>" {
                format!("{test}\0{job_id}")
            } else {
                test.clone()
            };
            let digest = Sha256::digest(format!("{repo}\0{head}\0{identity}").as_bytes());
            let key = format!("plangraph:CI-FIX-{digest:x}");
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
                class: class.name().into(),
                run_class: run_class.name().into(),
                job_id,
                head_sha: sha.to_owned(),
                current: true,
            };
            output.push(format!(
                "{}task: {}",
                if dry { "would upsert " } else { "" },
                serde_json::to_string(&task).map_err(|e| e.to_string())?
            ));
            made += 1;
            if !dry {
                store_task(&task_path, &mut tasks, task, &mut define, &mut output)?;
            }
        }
        let action = if made == 0 { "flake-rerun" } else { "fix-task" };
        let seen = format!("red:{run_id}:{}", run["run_attempt"].as_u64().unwrap_or(1));
        // One summary line per pull request run, the first time it is seen.
        if dry || !reruns.sightings.contains(&seen) {
            output.push(format!(
                "red: {head} run {run_id} class {} ({} job(s), {made} task(s), {action})",
                run_class.name(),
                jobs_seen.len()
            ));
        }
        if !dry && reruns.sightings.insert(seen) {
            report::append(
                dir,
                &report::RunRow {
                    run_id,
                    attempt: run["run_attempt"].as_u64().unwrap_or(1),
                    head: head.clone(),
                    head_sha: sha.to_owned(),
                    run_class: run_class.name().into(),
                    jobs: jobs_seen,
                    tasks: made,
                    action: action.into(),
                },
            )?;
            save(&dir.join("reruns.json"), &reruns)?;
        }
    }
    if !dry && !tasks.is_empty() {
        save_tasks(&task_path, &tasks)?;
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
    save_tasks(path, tasks)?;
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
    tasks.insert(key, task);
    save_tasks(path, tasks)?;
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
