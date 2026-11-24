//! `pg-spawnd step-run`: starts or resumes the step list of one batch from a saved steps
//! file. The list, its limits and its sink are the runner's; this module parses the call,
//! loads the saved rows, wires the real actions and reports how the list ended.
//!
//! ```text
//! pg-spawnd step-run RUN_ID --steps FILE --repo OWNER/NAME --batch-key KEY --items A,B
//!     --base SHA --brief FILE [--state DIR] [--branch NAME] [--check "ARGV"]...
//!     [--keeper-cmd "ARGV"] [--train-cmd "ARGV"] [--no-journal]
//! ```
//!
//! RUN_ID names the native run record (`record.json` and `native-request.json`) that holds
//! the worktree, the writer card and the review route. A missing steps file starts the list;
//! an existing one is validated and resumed where it stopped. Exit 0 when the part joined a
//! train, 3 when CI classified the head red, 1 otherwise; stdout is one JSON line.

use pg_spawnd::step_runner::{Inputs, Limits, Outcome, Runner};
use pg_spawnd::step_runner_log::StepLog;
use pg_worker_core::batch_steps::BatchSteps;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub(crate) const USAGE: &str = "usage: pg-spawnd step-run RUN_ID --steps FILE --repo OWNER/NAME --batch-key KEY --items A,B --base SHA --brief FILE [--state DIR] [--branch NAME] [--check ARGV]... [--keeper-cmd ARGV] [--train-cmd ARGV] [--no-journal]";

#[derive(Debug)]
pub(crate) struct Args {
    pub run_id: String,
    pub state: Option<String>,
    pub steps: PathBuf,
    pub inputs: Inputs,
    pub branch: Option<String>,
    pub checks: Vec<Vec<String>>,
    pub keeper_cmd: Option<Vec<String>>,
    pub train_cmd: Vec<String>,
    pub journal: bool,
}

fn words(text: &str) -> Vec<String> {
    text.split_whitespace().map(str::to_owned).collect()
}

pub(crate) fn parse(args: &[String]) -> Result<Args, String> {
    let mut args = args.iter().map(String::as_str);
    let run_id = args.next().ok_or(USAGE)?.to_owned();
    let (mut state, mut steps, mut repo, mut key, mut items, mut base, mut brief) =
        (None, None, None, None, None, None, None);
    let (mut branch, mut keeper_cmd) = (None, None);
    let (mut checks, mut train_cmd, mut journal) = (Vec::new(), Vec::new(), true);
    while let Some(flag) = args.next() {
        if flag == "--no-journal" {
            journal = false;
            continue;
        }
        let value = args.next().ok_or(USAGE)?.to_owned();
        let slot = match flag {
            "--state" => &mut state,
            "--steps" => &mut steps,
            "--repo" => &mut repo,
            "--batch-key" => &mut key,
            "--items" => &mut items,
            "--base" => &mut base,
            "--brief" => &mut brief,
            "--branch" => &mut branch,
            "--check" => {
                checks.push(words(&value));
                continue;
            }
            "--train-cmd" => {
                train_cmd = words(&value);
                continue;
            }
            "--keeper-cmd" => {
                keeper_cmd = Some(words(&value));
                continue;
            }
            _ => return Err(USAGE.into()),
        };
        if slot.replace(value).is_some() {
            return Err(format!("{flag} given twice"));
        }
    }
    let need = |value: Option<String>, flag: &str| value.ok_or(format!("{flag} is required"));
    let items: Vec<String> = need(items, "--items")?
        .split(',')
        .map(str::to_owned)
        .collect();
    if items.iter().any(|item| item.trim().is_empty()) {
        return Err("--items holds an empty key".into());
    }
    let brief = need(brief, "--brief")?;
    // The brief is read by the write step, possibly after a resume from another directory.
    let brief = std::fs::canonicalize(&brief)
        .map_err(|err| format!("brief {brief}: {err}"))?
        .to_string_lossy()
        .into_owned();
    Ok(Args {
        run_id,
        state,
        steps: PathBuf::from(need(steps, "--steps")?),
        inputs: Inputs {
            repo: need(repo, "--repo")?,
            batch_key: need(key, "--batch-key")?,
            items,
            base: need(base, "--base")?,
            brief,
        },
        branch,
        checks,
        keeper_cmd,
        train_cmd,
        journal,
    })
}

/// The limits of a batch: attempts per step from the runner, fix rounds from the round cap.
pub(crate) fn limits() -> Limits {
    Limits {
        fix_rounds: crate::round_progress::MAX_ROUNDS as u32,
        ..Limits::default()
    }
}

/// The runner for `inputs`: new when the steps file is missing, else resumed from its rows.
pub(crate) fn load(path: &Path, inputs: Inputs) -> Result<Runner, String> {
    match std::fs::read(path) {
        Ok(bytes) => {
            let steps: BatchSteps = serde_json::from_slice(&bytes)
                .map_err(|err| format!("steps file {}: {err}", path.display()))?;
            Runner::resume(inputs, limits(), steps)
                .map_err(|err| format!("steps file {}: {err}", path.display()))
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Runner::new(inputs, limits())),
        Err(err) => Err(format!("steps file {}: {err}", path.display())),
    }
}

/// The exit code and the one-line report of an outcome.
pub(crate) fn report(outcome: &Outcome, runner: &Runner) -> (i32, Value) {
    let rows = runner.steps().rows.len();
    let (code, mut line) = match outcome {
        Outcome::Joined { train, manifest } => (
            0,
            json!({"outcome":"joined","train":train,"manifest":manifest}),
        ),
        Outcome::CiRed { head, pr } => (3, json!({"outcome":"ci_red","head":head,"pr":pr})),
        Outcome::Failed {
            step,
            step_index,
            reason,
        } => (
            1,
            json!({"outcome":"failed","step":step,"step_index":step_index,"reason":reason}),
        ),
        Outcome::FixRoundsExhausted { rounds } => {
            (1, json!({"outcome":"fix_rounds_exhausted","rounds":rounds}))
        }
        Outcome::NotSaved { step_index, reason } => (
            1,
            json!({"outcome":"not_saved","step_index":step_index,"reason":reason}),
        ),
        Outcome::Invalid {
            step,
            step_index,
            reason,
        } => (
            1,
            json!({"outcome":"invalid","step":step,"step_index":step_index,"reason":reason}),
        ),
    };
    line["rows"] = json!(rows);
    (code, line)
}

pub(crate) fn log_for(args: &Args) -> StepLog {
    StepLog {
        path: args.steps.clone(),
        journal_repo: args.journal.then(|| args.inputs.repo.clone()),
        item_keys: args.inputs.items.clone(),
        base: args.inputs.base.clone(),
    }
}

pub(crate) fn clock() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// `pg-spawnd step-run`: run the list to its end and print the outcome as one JSON line.
pub(crate) fn execute(args: Vec<String>, out: &mut dyn std::io::Write) -> Result<(), String> {
    use pg_spawnd::step_runner_shell::{Composite, ShellConfig, ShellSteps, SystemRunner};
    use pg_spawnd::step_services::{CiWait, ServiceSteps};
    use pg_spawnd::step_services_cli::{GhCi, KeeperCli, TrainCommand};

    let args = parse(&args)?;
    let state = crate::state::resolve_state(args.state.as_deref(), |key| std::env::var_os(key))?;
    let findings = args.steps.with_extension("findings");
    let host =
        crate::native_writer_host::step_host(&state, &args.run_id, &args.inputs.base, findings)?;
    let inputs = &args.inputs;
    let shell = ShellSteps {
        config: ShellConfig {
            repo_dir: host.repo.clone(),
            worktree_dir: host.worktree.clone(),
            branch: args
                .branch
                .clone()
                .unwrap_or_else(|| format!("lane/{}", inputs.batch_key)),
            checks: args.checks.clone(),
            pr_title: inputs.batch_key.clone(),
            pr_body: format!("Items: {}", inputs.items.join(", ")),
        },
        runner: SystemRunner,
    };
    let services = ServiceSteps {
        keeper: KeeperCli {
            runner: SystemRunner,
            dir: host.repo.clone(),
            prefix: args
                .keeper_cmd
                .clone()
                .unwrap_or_else(|| vec!["pg".into(), "--json".into()]),
        },
        ci: GhCi {
            runner: SystemRunner,
            dir: host.repo.clone(),
            repo: inputs.repo.clone(),
        },
        train: TrainCommand {
            runner: SystemRunner,
            dir: host.repo.clone(),
            argv: args.train_cmd.clone(),
        },
        wait: CiWait::default(),
        sleep: Box::new(std::thread::sleep),
        rest: host.agents,
    };
    let mut actions = Composite {
        shell,
        rest: services,
    };
    let mut runner = load(&args.steps, args.inputs.clone())?;
    let mut sink = log_for(&args);
    let outcome = runner.run(&mut actions, &mut sink, &mut clock);
    let (code, line) = report(&outcome, &runner);
    writeln!(out, "{line}").map_err(|err| err.to_string())?;
    if code != 0 {
        let _ = out.flush();
        std::process::exit(code);
    }
    Ok(())
}
