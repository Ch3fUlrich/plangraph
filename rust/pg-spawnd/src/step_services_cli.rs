//! Command backed implementations of the service traits of `step_services`: the keeper CLI,
//! the GitHub CLI and the train desk command. Each runs one command through `CommandRunner`
//! and reads its JSON, so the same fake runner that tests the shell steps tests these.

use crate::step_runner_shell::CommandRunner;
use crate::step_services::{CiRun, CiRuns, Keeper, TrainDesk};
use pg_worker_core::batch_steps::TrainJoin;
use serde_json::Value;
use std::path::PathBuf;

fn json(text: &str, what: &str) -> Result<Value, String> {
    serde_json::from_str(text).map_err(|err| format!("{what}: output is not JSON: {err}"))
}

fn text<'a>(value: &'a Value, field: &str, what: &str) -> Result<&'a str, String> {
    value[field]
        .as_str()
        .filter(|found| !found.trim().is_empty())
        .ok_or_else(|| format!("{what}: no {field} in {value}"))
}

/// The keeper CLI (`prefix` is the argv that runs it with JSON output, for example
/// `python3 -m plangraph.keeper.cli --json`).
pub struct KeeperCli<R: CommandRunner> {
    pub runner: R,
    pub dir: PathBuf,
    pub prefix: Vec<String>,
}

impl<R: CommandRunner> KeeperCli<R> {
    fn argv(&self, verb: &str, args: &[String]) -> Vec<String> {
        let mut argv = self.prefix.clone();
        argv.push(verb.to_owned());
        argv.extend(args.iter().cloned());
        argv
    }
}

impl<R: CommandRunner> Keeper for KeeperCli<R> {
    fn claim_batch(&mut self, keys: &[String]) -> Result<Vec<(String, u64)>, String> {
        let claim = self.argv("claim-batch", keys);
        let answer = match self.runner.run(&self.dir, &claim) {
            Ok(answer) => answer,
            Err(error) => {
                // The CLI exits non-zero when one key is refused and the others stay claimed,
                // and its output is not kept then: release every key so none is left held.
                // A key this host does not hold is refused its release; that is the point.
                for key in keys {
                    let release = self.argv("release", std::slice::from_ref(key));
                    let _ = self.runner.run(&self.dir, &release);
                }
                return Err(format!("claim-batch: {error}"));
            }
        };
        let rows = json(&answer, "claim-batch")?;
        let rows = rows["results"]
            .as_array()
            .ok_or("claim-batch: no results array")?;
        rows.iter()
            .map(|row| {
                let key = text(row, "key", "claim-batch")?;
                let epoch = row["epoch"]
                    .as_u64()
                    .ok_or_else(|| format!("claim-batch: {key} refused: {row}"))?;
                Ok((key.to_owned(), epoch))
            })
            .collect()
    }
}

/// The workflow runs of a head, read with `gh api`.
pub struct GhCi<R: CommandRunner> {
    pub runner: R,
    pub dir: PathBuf,
    /// `OWNER/NAME`.
    pub repo: String,
}

impl<R: CommandRunner> CiRuns for GhCi<R> {
    fn runs(&mut self, head: &str) -> Result<Vec<CiRun>, String> {
        let endpoint = format!(
            "repos/{}/actions/runs?head_sha={head}&per_page=100",
            self.repo
        );
        let argv = ["gh", "api", &endpoint].map(str::to_owned).to_vec();
        let answer = json(&self.runner.run(&self.dir, &argv)?, "gh api runs")?;
        let runs = answer["workflow_runs"]
            .as_array()
            .ok_or("gh api runs: no workflow_runs array")?;
        runs.iter()
            .map(|run| {
                Ok(CiRun {
                    id: run["id"].as_u64().ok_or("gh api runs: run without id")?,
                    attempt: run["run_attempt"]
                        .as_u64()
                        .and_then(|attempt| u32::try_from(attempt).ok())
                        .filter(|attempt| *attempt > 0)
                        .ok_or("gh api runs: run without attempt")?,
                    status: text(run, "status", "gh api runs")?.to_owned(),
                    conclusion: run["conclusion"].as_str().map(str::to_owned),
                })
            })
            .collect()
    }
}

/// The train desk of the site: a command that takes `--head`, `--pr`, `--run` and `--attempt`
/// after its own argv and prints `{"train":..,"manifest":..}` for the train the part joined.
pub struct TrainCommand<R: CommandRunner> {
    pub runner: R,
    pub dir: PathBuf,
    pub argv: Vec<String>,
}

impl<R: CommandRunner> TrainDesk for TrainCommand<R> {
    fn join(&mut self, head: &str, pr: u64, run: u64, attempt: u32) -> Result<TrainJoin, String> {
        if self.argv.is_empty() {
            return Err("no train join command configured".into());
        }
        let mut argv = self.argv.clone();
        for (flag, value) in [
            ("--head", head.to_owned()),
            ("--pr", pr.to_string()),
            ("--run", run.to_string()),
            ("--attempt", attempt.to_string()),
        ] {
            argv.extend([flag.to_owned(), value]);
        }
        let answer = json(&self.runner.run(&self.dir, &argv)?, "train join")?;
        Ok(TrainJoin {
            train: text(&answer, "train", "train join")?.to_owned(),
            manifest: text(&answer, "manifest", "train join")?.to_owned(),
        })
    }
}
