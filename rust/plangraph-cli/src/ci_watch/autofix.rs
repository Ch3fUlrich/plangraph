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
/// Paths a Hand-Edit commit never touches (the gate files of tools/ci_commit_gate.py).
const GATE_PATHS: &[&str] = &[
    ".github/workflows/",
    "tools/ci_commit_gate.py",
    "tools/ci_commit_exempt.txt",
    "tools/gate_wiring_check.py",
];

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

/// Clone the head into `scratch` (an empty or missing directory), run the fix, commit and push once.
pub fn apply(scratch: &Path, request: &Request) -> Result<Outcome> {
    let head = request.head;
    let parent = scratch.parent().unwrap_or(scratch);
    let target = scratch.to_string_lossy();
    run(
        parent,
        "git",
        &[
            "clone",
            "--quiet",
            "--branch",
            &head.branch,
            &head.remote,
            &target,
        ],
    )?;
    if run(scratch, "git", &["rev-parse", "HEAD"])?.trim() != head.sha {
        return Ok(Outcome::Stale);
    }
    let (program, args) = request.command.split_first().ok_or("empty fix command")?;
    run(&scratch.join(request.dir), program, args)?;
    if run(scratch, "git", &["status", "--porcelain"])?
        .trim()
        .is_empty()
    {
        return Ok(Outcome::Unchanged);
    }
    // Stage first: a file the command created is untracked and invisible to a plain diff.
    run(scratch, "git", &["add", "-A"])?;
    let paths = run(scratch, "git", &["diff", "--cached", "--name-only"])?;
    if let Some(path) = paths
        .lines()
        .find(|p| GATE_PATHS.iter().any(|gate| p.starts_with(gate)))
    {
        return Ok(Outcome::GatePath(path.to_owned()));
    }
    let lines: u64 = run(scratch, "git", &["diff", "--cached", "--numstat"])?
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
        scratch,
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
    run(scratch, "git", &["push", "--quiet", "origin", &refspec])?;
    Ok(Outcome::Pushed(
        run(scratch, "git", &["rev-parse", "HEAD"])?
            .trim()
            .to_owned(),
    ))
}

/// What the watcher did for each (pull request, class): kept so a head gets one automatic fix per class.
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
    tasks: &BTreeMap<String, Task>,
    mut lookup: impl FnMut(u64) -> Result<PrHead>,
    mut fix: impl FnMut(&Request, &Path) -> Result<Outcome>,
) -> Result<Vec<String>> {
    let path = dir.join("autofix.json");
    let mut ledger: Ledger = super::read(&path)?;
    let mut rows = Vec::new();
    let mut heads: BTreeMap<u64, Result<PrHead>> = BTreeMap::new();
    for task in tasks.values() {
        let (Some(class), Some(pr)) = (RedClass::from_name(&task.class), task.pr) else {
            continue;
        };
        if ledger.closed.contains(&pr) {
            continue;
        }
        let fixed: BTreeSet<(String, RedClass)> = ledger
            .done
            .keys()
            .filter_map(|k| {
                let (head, class) = k.split_once(':')?;
                Some((head.to_owned(), RedClass::from_name(class)?))
            })
            .collect();
        let Action::AutoFix {
            command,
            dir: work,
            trailer,
        } = classify::decide(class, &task.head, task.job_id, false, &fixed)
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
        let key = format!("{}:{}", task.head, class.name());
        ledger
            .done
            .insert(key.clone(), format!("job {}: started", task.job_id));
        super::save(&path, &ledger)?;
        let outcome = {
            let scratch = dir.join("autofix-scratch");
            let _ = std::fs::remove_dir_all(&scratch);
            let subject = format!(
                "Apply the {} fix of failed job {}",
                class.name(),
                task.job_id
            );
            let result = fix(
                &Request {
                    head: &head,
                    command,
                    dir: work,
                    trailer: &trailer,
                    subject: &subject,
                },
                &scratch,
            );
            let _ = std::fs::remove_dir_all(&scratch);
            result
        };
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
