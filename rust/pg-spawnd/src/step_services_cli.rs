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

impl<R: CommandRunner> KeeperCli<R> {
    /// Release one claim of this attempt at the epoch it was granted: the CLI would otherwise
    /// look up the current epoch, which may belong to a claim another batch holds.
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String> {
        let mut argv = vec!["env".to_owned(), format!("PG_EPOCH={epoch}")];
        argv.extend(self.argv("release", &[key.to_owned()]));
        self.runner
            .run(&self.dir, &argv)
            .map(drop)
            .map_err(|err| format!("{key}@{epoch}: {err}"))
    }
}

impl<R: CommandRunner> Keeper for KeeperCli<R> {
    fn claim_batch(&mut self, keys: &[String]) -> Result<Vec<(String, u64)>, String> {
        let claim = self.argv("claim-batch", keys);
        // The CLI exits non-zero when a row is refused but still prints every row.
        let answer = self
            .runner
            .run_lenient(&self.dir, &claim)
            .map_err(|err| format!("claim-batch: {err}; outcome unknown, keys may be held"))?;
        let rows = json(&answer, "claim-batch")?;
        let rows = rows["results"]
            .as_array()
            .ok_or("claim-batch: no results array")?;
        let (mut held, mut refused) = (Vec::new(), Vec::new());
        for row in rows {
            let key = text(row, "key", "claim-batch")?.to_owned();
            match row["epoch"].as_u64() {
                Some(epoch) => held.push((key, epoch)),
                None => refused.push(format!("{key}: {row}")),
            }
        }
        if refused.is_empty() {
            return Ok(held);
        }
        // Give back what this attempt got, at the epochs it got, and say what could not be.
        let failures: Vec<String> = held
            .iter()
            .filter_map(|(key, epoch)| self.release(key, *epoch).err())
            .collect();
        let mut error = format!("claim-batch refused {}", refused.join("; "));
        if !failures.is_empty() {
            error.push_str(&format!("; release failed: {}", failures.join("; ")));
        }
        Err(error)
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
    fn runs(&mut self, head: &str, pr: u64) -> Result<Vec<CiRun>, String> {
        let endpoint = format!(
            "repos/{}/actions/runs?head_sha={head}&per_page=100",
            self.repo
        );
        let argv = ["gh", "api", &endpoint].map(str::to_owned).to_vec();
        let answer = json(&self.runner.run(&self.dir, &argv)?, "gh api runs")?;
        let runs = answer["workflow_runs"]
            .as_array()
            .ok_or("gh api runs: no workflow_runs array")?;
        // Another pull request on the same commit has its own runs; only this one's count.
        let of_pr = |run: &&Value| {
            run["pull_requests"]
                .as_array()
                .is_some_and(|prs| prs.iter().any(|found| found["number"].as_u64() == Some(pr)))
        };
        runs.iter()
            .filter(of_pr)
            .map(|run| {
                Ok(CiRun {
                    name: text(run, "name", "gh api runs")?.to_owned(),
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
