//! The automatic fix of a mechanical red (fmt, clippy): a scratch clone of the pull request head, the fix
//! command, one `Hand-Edit` commit naming the failed job, one plain push. The fix task of rule 3.46 is made
//! by the scan before this runs and stays; a refused or failed fix leaves it to its writer.
use super::classify::{self, Action, RedClass};
use super::github::Github;
use super::{Result, Task};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::process::Command;
use std::time::Duration;

/// A Hand-Edit commit may change at most this many lines (tools/ci_commit_gate.py HAND_EDIT_MAX_LINES).
pub const MAX_LINES: u64 = 50;
/// Paths a Hand-Edit commit never touches: tools/ci_commit_gate.py `AUTH_DOOR_PATHS` and `GATE_PATHS`, as
/// fnmatch patterns (`*` matches any characters, `/` included). A test holds this list equal to that file.
pub(super) const PROTECTED: &[&str] = &[
    "src/plangraph/keeper/*",
    "src/plangraph/*/auth*",
    "src/plangraph/*auth*",
    "*/roles*",
    "*/scopes*",
    "roles*",
    "scopes*",
    "tools/seat_verdict.py*",
    "tools/seat_record.py*",
    "ops/pg.sh*",
    "tools/ci_commit_gate.py*",
    "tools/ci_commit_exempt.txt*",
    "tools/gate_wiring_check.py*",
    ".github/*",
    ".github/workflows/*",
    "rust/plangraph-cli/src/land/go_verify.rs*",
    ".gitattributes",
    "*/.gitattributes",
];

/// `fnmatch` for patterns that use only `*`.
fn glob(pattern: &str, text: &str) -> bool {
    match pattern.split_once('*') {
        None => pattern == text,
        Some((head, rest)) => text.strip_prefix(head).is_some_and(|tail| {
            (0..=tail.len())
                .filter(|at| tail.is_char_boundary(*at))
                .any(|at| glob(rest, &tail[at..]))
        }),
    }
}

fn protected(path: &str) -> bool {
    PROTECTED.iter().any(|pattern| glob(pattern, path))
}

/// The pull request head a fix applies to.
#[derive(Clone)]
pub struct PrHead {
    pub remote: String,
    pub branch: String,
    pub sha: String,
}

#[derive(Debug, Eq, PartialEq)]
pub enum Outcome {
    Pushed(String),
    /// The command changed nothing: the red is not what the class said.
    Unchanged,
    TooLarge(u64),
    GatePath(String),
    /// The pull request head moved since the red run; a newer run decides.
    Stale,
}

pub struct Request<'a> {
    pub head: &'a PrHead,
    pub command: &'a [&'a str],
    pub dir: &'a str,
    pub trailer: &'a str,
    pub subject: &'a str,
}

fn run(dir: &Path, program: &str, args: &[&str]) -> Result<String> {
    let mut command = Command::new(program);
    command.args(args).current_dir(dir);
    pg_worker_core::host_command::isolate_git(&mut command);
    let bytes = pg_worker_core::host_command::output(&mut command, Duration::from_secs(900))
        .map_err(|_| format!("{program} {} failed", args.first().unwrap_or(&"")))?;
    String::from_utf8(bytes).map_err(|_| "non-UTF8 command output".into())
}

/// Clone the head into `scratch`, run the fix in a sandbox copy, and have the trusted checkout stage that copy's
/// files, check them, commit and push once. The sandbox copy is read as plain files: its `.git` (config, hooks)
/// is never run, so a build script cannot turn the commit or the push into code of its own.
pub fn apply(scratch: &Path, request: &Request) -> Result<Outcome> {
    let head = request.head;
    std::fs::create_dir_all(scratch).map_err(|e| e.to_string())?;
    let (trusted, sandbox) = (scratch.join("trusted"), scratch.join("sandbox"));
    run(
        scratch,
        "git",
        &[
            "clone",
            "--quiet",
            "--branch",
            &head.branch,
            &head.remote,
            &trusted.to_string_lossy(),
        ],
    )?;
    if run(&trusted, "git", &["rev-parse", "HEAD"])?.trim() != head.sha {
        return Ok(Outcome::Stale);
    }
    run(
        scratch,
        "git",
        &[
            "clone",
            "--quiet",
            "--local",
            "--no-hardlinks",
            &trusted.to_string_lossy(),
            &sandbox.to_string_lossy(),
        ],
    )?;
    let (program, args) = request.command.split_first().ok_or("empty fix command")?;
    super::autofix_jail::run(&sandbox, request.dir, program, args, scratch)?;
    // The trusted repository, the sandbox's files: `add` records what the command left, whatever its `.git` says.
    let (git_dir, work_tree) = (trusted.join(".git"), sandbox.to_string_lossy().into_owned());
    let git_dir = git_dir.to_string_lossy().into_owned();
    let staged = |args: &[&str]| -> Result<String> {
        let mut all = vec!["--git-dir", &git_dir, "--work-tree", &work_tree];
        all.extend_from_slice(args);
        run(&trusted, "git", &all)
    };
    staged(&["add", "-A"])?;
    let paths = run(&trusted, "git", &["diff", "--cached", "--name-only"])?;
    if paths.trim().is_empty() {
        return Ok(Outcome::Unchanged);
    }
    if let Some(path) = paths.lines().find(|p| protected(p)) {
        return Ok(Outcome::GatePath(path.to_owned()));
    }
    let lines: u64 = run(&trusted, "git", &["diff", "--cached", "--numstat"])?
        .lines()
        .filter_map(|l| {
            let mut f = l.split_whitespace();
            Some(f.next()?.parse::<u64>().ok()? + f.next()?.parse::<u64>().ok()?)
        })
        .sum();
    if lines > MAX_LINES {
        return Ok(Outcome::TooLarge(lines));
    }
    let message = format!(
        "{}\n\n{}\nChangelog: skip (ci-watch automatic fix)\n",
        request.subject, request.trailer
    );
    run(
        &trusted,
        "git",
        &[
            "-c",
            "user.name=plangraph ci-watch",
            "-c",
            "user.email=ci-watch@invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            &message,
        ],
    )?;
    let refspec = format!("HEAD:refs/heads/{}", head.branch);
    run(&trusted, "git", &["push", "--quiet", "origin", &refspec])?;
    Ok(Outcome::Pushed(
        run(&trusted, "git", &["rev-parse", "HEAD"])?
            .trim()
            .to_owned(),
    ))
}

/// What the watcher did for each (repository, head SHA, class): a head gets one automatic fix per class, and
/// the head a fix produced is entered too, so a fix that stays red is never fixed again (no loop).
#[derive(Default, Deserialize, Serialize)]
pub struct Ledger {
    pub done: BTreeMap<String, String>,
    /// Pull requests found closed or merged: never read or touched again.
    #[serde(default)]
    pub closed: BTreeSet<u64>,
}

/// What `pr_head` answers for a pull request that is no longer open.
pub const NOT_OPEN: &str = "pull request is not open";

/// Run the automatic fixes the tasks call for. `lookup` reads a pull request's head, `fix` applies one.
/// A (pull request, class) is reserved before the fix runs, so a crash or a refusal never repeats it.
pub fn pass(
    dir: &Path,
    repo: &str,
    tasks: &BTreeMap<String, Task>,
    mut lookup: impl FnMut(u64) -> Result<PrHead>,
    mut still_red: impl FnMut(&str) -> Result<bool>,
    mut fix: impl FnMut(&Request, &Path) -> Result<Outcome>,
) -> Result<Vec<String>> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    // One pass at a time: two overlapping passes never claim the same work.
    let _lock = crate::atomic::lock(&dir.join("autofix.lock")).map_err(|e| e.to_string())?;
    let path = dir.join("autofix.json");
    let mut ledger: Ledger = super::read(&path)?;
    let mut rows = Vec::new();
    let mut heads: BTreeMap<u64, Result<PrHead>> = BTreeMap::new();
    // Only the failures of the newest scan: a head a rerun turned green has no current task.
    for task in tasks.values().filter(|task| task.current) {
        let (Some(class), Some(pr)) = (RedClass::from_name(&task.class), task.pr) else {
            continue;
        };
        if ledger.closed.contains(&pr) {
            continue;
        }
        let label = format!("{repo}@{}", task.head_sha);
        let fixed: BTreeSet<(String, RedClass)> = ledger
            .done
            .keys()
            .filter_map(|k| {
                let (head, class) = k.rsplit_once(':')?;
                Some((head.to_owned(), RedClass::from_name(class)?))
            })
            .collect();
        let Action::AutoFix {
            command,
            dir: work,
            trailer,
        } = classify::decide(class, &label, task.job_id, false, &fixed)
        else {
            continue;
        };
        let head = match heads.entry(pr).or_insert_with(|| lookup(pr)).clone() {
            Ok(head) => head,
            Err(error) if error == NOT_OPEN => {
                ledger.closed.insert(pr);
                super::save(&path, &ledger)?;
                continue;
            }
            Err(error) => {
                rows.push(format!(
                    "autofix {} {}: lookup failed: {}",
                    task.head,
                    class.name(),
                    super::logs::scrub(&error)
                ));
                continue;
            }
        };
        if head.sha != task.head_sha {
            continue; // a newer head has its own run; nothing is reserved
        }
        // The tasks were read before this pass took the lock: a rerun or a concurrent scan may have turned the
        // run green since, so its outcome is read again at the source before anything is reserved or pushed.
        match still_red(&task.head_sha) {
            Ok(true) => {}
            Ok(false) => continue,
            Err(error) => {
                rows.push(format!(
                    "autofix {} {}: run state unreadable: {}",
                    task.head,
                    class.name(),
                    super::logs::scrub(&error)
                ));
                continue;
            }
        }
        let key = format!("{label}:{}", class.name());
        ledger
            .done
            .insert(key.clone(), format!("job {}: started", task.job_id));
        super::save(&path, &ledger)?;
        let outcome = {
            // A directory of its own for each attempt, removed when the attempt ends.
            let attempt = tempfile::Builder::new()
                .prefix("autofix-")
                .tempdir_in(dir)
                .map_err(|e| e.to_string())?;
            let scratch = attempt.path().to_path_buf();
            let subject = format!(
                "Apply the {} fix of failed job {}",
                class.name(),
                task.job_id
            );
            fix(
                &Request {
                    head: &head,
                    command,
                    dir: work,
                    trailer: &trailer,
                    subject: &subject,
                },
                &scratch,
            )
        };
        if let Ok(Outcome::Pushed(new)) = &outcome {
            ledger.done.insert(
                format!("{repo}@{new}:{}", class.name()),
                format!("produced by the fix of job {}", task.job_id),
            );
        }
        let text = match &outcome {
            Ok(Outcome::Pushed(sha)) => format!("pushed {sha}"),
            Ok(Outcome::Unchanged) => "unchanged; left to the fix writer".into(),
            Ok(Outcome::TooLarge(n)) => format!("{n} lines; left to the fix writer"),
            Ok(Outcome::GatePath(p)) => format!("touches {p}; left to the fix writer"),
            Ok(Outcome::Stale) => "head moved; no fix".into(),
            Err(error) => format!(
                "failed: {}; left to the fix writer",
                super::logs::scrub(error)
            ),
        };
        if matches!(outcome, Ok(Outcome::Stale)) {
            ledger.done.remove(&key);
        } else {
            ledger
                .done
                .insert(key, format!("job {}: {text}", task.job_id));
        }
        super::save(&path, &ledger)?;
        rows.push(format!(
            "autofix {} {}: job {}: {text}",
            task.head,
            class.name(),
            task.job_id
        ));
    }
    Ok(rows)
}

/// True while the newest pull-request run of the workflow on head commit `sha` has concluded red. A newer run on
/// the same commit (a rerun, a run a label or an edit started) decides, whatever run the task came from; a run
/// in flight has no conclusion and is not red.
pub fn head_is_red(gh: &mut impl Github, sha: &str) -> Result<bool> {
    let runs = gh.page(
        &format!("actions/workflows/tests.yml/runs?head_sha={sha}&event=pull_request&per_page=30"),
        Some("workflow_runs"),
    )?;
    let newest = runs
        .iter()
        .filter(|run| run["head_sha"] == sha)
        .max_by_key(|run| run["id"].as_u64().unwrap_or(0))
        .ok_or("no run of this head")?;
    Ok(matches!(
        newest["conclusion"].as_str(),
        Some("failure" | "timed_out" | "startup_failure")
    ))
}

/// The head of pull request `pr` as GitHub reports it; a closed or merged pull request answers `NOT_OPEN` and a
/// head from a fork is refused.
pub fn pr_head(gh: &mut impl Github, repo: &str, pr: u64) -> Result<PrHead> {
    let value = gh.value(&format!("pulls/{pr}"))?;
    if value["state"] != "open" {
        return Err(NOT_OPEN.into());
    }
    let full = value["head"]["repo"]["full_name"]
        .as_str()
        .unwrap_or_default();
    if full != repo {
        return Err("pull request head is not in this repository".into());
    }
    Ok(PrHead {
        remote: format!("https://github.com/{repo}.git"),
        branch: value["head"]["ref"]
            .as_str()
            .ok_or("no head ref")?
            .to_owned(),
        sha: value["head"]["sha"]
            .as_str()
            .ok_or("no head sha")?
            .to_owned(),
    })
}
