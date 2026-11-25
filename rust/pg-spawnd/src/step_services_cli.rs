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
    /// Holds the idempotency key of the claim between the call and the saved row. A process
    /// killed after the keeper granted the claim retries with the same key, and the keeper
    /// answers from its receipt instead of refusing the keys as already claimed.
    pub key_file: PathBuf,
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
    /// The key of this batch's claim: the saved one, or a new one saved before it is used.
    fn claim_key(&self, keys: &[String]) -> Result<String, String> {
        if let Ok(saved) = std::fs::read_to_string(&self.key_file) {
            if !saved.trim().is_empty() {
                return Ok(saved.trim().to_owned());
            }
        }
        use sha2::{Digest, Sha256};
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let digest = Sha256::digest(format!("{keys:?}:{now}:{}", std::process::id()));
        let key: String = digest.iter().take(16).map(|b| format!("{b:02x}")).collect();
        std::fs::write(&self.key_file, &key)
            .map_err(|err| format!("claim key not saved at {}: {err}", self.key_file.display()))?;
        Ok(key)
    }

    /// Release one claim of this attempt at the epoch it was granted: the CLI would otherwise
    /// look up the current epoch, which may belong to a claim another batch holds. A release
    /// whose answer was lost after the keeper committed it is refused on replay (the item is
    /// no longer claimed), so a failed release is confirmed against the item: not claimed, or
    /// claimed at another epoch, means this attempt's grant is gone.
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String> {
        let mut argv = vec!["env".to_owned(), format!("PG_EPOCH={epoch}")];
        argv.extend(self.argv("release", &[key.to_owned()]));
        let Err(error) = self.runner.run(&self.dir, &argv) else {
            return Ok(());
        };
        let show = self.argv("show", &[key.to_owned()]);
        let shown = self
            .runner
            .run_lenient(&self.dir, &show)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok());
        let still_held = match &shown {
            Some(item) => match (item["state"].as_str(), item["epoch"].as_u64()) {
                (Some("claimed"), Some(held)) => held == epoch,
                (Some(_), Some(_)) => false,
                _ => true,
            },
            None => true,
        };
        if still_held {
            return Err(format!("{key}@{epoch}: {error}"));
        }
        Ok(())
    }
}

impl<R: CommandRunner> Keeper for KeeperCli<R> {
    fn claim_batch(&mut self, keys: &[String]) -> Result<Vec<(String, u64)>, String> {
        let key = self.claim_key(keys)?;
        let mut claim = vec!["env".to_owned(), format!("PG_IDEMPOTENCY_KEY={key}")];
        claim.extend(self.argv("claim-batch", keys));
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
        // The key is dropped only once the cleanup is confirmed: a kill before that retries
        // with the same key, the keeper replays the mixed answer, and the cleanup runs again.
        if failures.is_empty() {
            let _ = std::fs::remove_file(&self.key_file);
        }
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
    fn draft_lane_part(&mut self, pr: u64, head: &str) -> Result<bool, String> {
        let pull = self.pull(pr)?;
        // The draft flag and branch are those of the pull request's current head; they say
        // nothing about a run of an older head.
        if pull["head"]["sha"].as_str() != Some(head) {
            return Ok(false);
        }
        let draft = pull["draft"]
            .as_bool()
            .ok_or("gh api pull: no draft flag")?;
        let branch = text(&pull["head"], "ref", "gh api pull")?;
        Ok(draft && branch.starts_with("lane/"))
    }
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
                    event: text(run, "event", "gh api runs")?.to_owned(),
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

impl<R: CommandRunner> GhCi<R> {
    fn pull(&mut self, pr: u64) -> Result<Value, String> {
        let endpoint = format!("repos/{}/pulls/{pr}", self.repo);
        let argv = ["gh", "api", &endpoint].map(str::to_owned).to_vec();
        json(&self.runner.run(&self.dir, &argv)?, "gh api pull")
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
