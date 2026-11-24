//! The agent steps of the batch step list (write, review, fix) over the native writer
//! `Driver`: write is the first writer turn, review is one review run on the exact head, fix
//! is a turn of the same writer session. The step runner owns the order, the attempts and
//! the round cap; this adapter owns only what a turn needs, so the loop of `native_writer`
//! and the runner do not both drive a batch.

use crate::native_writer::{quota, Driver};
use crate::writer_control::STOPPED;
use pg_spawnd::step_runner::StepActions;
use pg_worker_core::batch_steps::{
    CiClass, Claim, FastChecks, Fix, Push, Review, TrainJoin, Verdict, Worktree, Write,
};
use serde_json::Value;
use std::path::PathBuf;

pub(crate) struct AgentSteps<D: Driver> {
    pub driver: D,
    /// The base the batch was cut from: a writer turn must commit beyond it.
    pub base: String,
    /// Where the text of every review is kept, one file per review run, so a fix turn of a
    /// resumed list still reads the findings its `review` reference names.
    pub findings: PathBuf,
    /// A usage limit or a stop ends the batch at once: another turn only spends the quota.
    halted: Option<String>,
}

impl<D: Driver> AgentSteps<D> {
    pub(crate) fn new(driver: D, base: String, findings: PathBuf) -> Self {
        Self {
            driver,
            base,
            findings,
            halted: None,
        }
    }

    /// One writer turn. A usage limit is booked in the leg state and halts later turns.
    fn turn(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        if let Some(reason) = &self.halted {
            return Err(reason.clone());
        }
        self.driver.execute(session, prompt).map_err(|error| {
            if let Some(limit) = quota::from_error(&error) {
                let _ = self.driver.record_quota(&limit);
                self.halted = Some(error.clone());
            } else if error == STOPPED {
                self.halted = Some(error.clone());
            }
            error
        })
    }

    /// The review state that must outlive the process: the last REQUEST_CHANGES review, for
    /// the delta review of the next round, and the HIGH+MED count of every round, for the
    /// stop rule.
    fn state_path(&self, name: &str) -> PathBuf {
        self.findings.join(name)
    }

    fn previous(&self) -> Option<Value> {
        let text = std::fs::read_to_string(self.state_path("previous.json")).ok()?;
        serde_json::from_str(&text).ok()
    }

    fn rounds(&self) -> Vec<(String, u64)> {
        std::fs::read_to_string(self.state_path("rounds.json"))
            .ok()
            .and_then(|text| serde_json::from_str::<Vec<(String, u64)>>(&text).ok())
            .unwrap_or_default()
    }

    fn keep(&self, name: &str, value: &impl serde::Serialize) -> Result<(), String> {
        std::fs::create_dir_all(&self.findings).map_err(|err| err.to_string())?;
        let bytes = serde_json::to_vec(value).map_err(|err| err.to_string())?;
        std::fs::write(self.state_path(name), bytes)
            .map_err(|err| format!("review state {name} not kept: {err}"))
    }

    /// The stamp the push may consume: the one this driver wrote for exactly `head`.
    pub(crate) fn verify_stamp(&mut self, head: &str, stamp: &str) -> Result<(), String> {
        match self.driver.stamp_path(head) {
            Some(expected) if expected.is_file() && expected == std::path::Path::new(stamp) => {
                Ok(())
            }
            _ => Err(format!("no fast-checks stamp for {head}")),
        }
    }

    fn findings_path(&self, run: &str) -> Result<PathBuf, String> {
        let plain = !run.is_empty()
            && run
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_');
        if !plain {
            return Err(format!("review run id {run:?} is not a plain id"));
        }
        Ok(self.findings.join(format!("{run}.txt")))
    }
}

impl<D: Driver> StepActions for AgentSteps<D> {
    /// `brief` is the path of the brief file; its text is the writer's first prompt.
    fn write(&mut self, _worktree: &str, brief: &str) -> Result<Write, String> {
        let text = std::fs::read_to_string(brief).map_err(|err| format!("brief {brief}: {err}"))?;
        let skill = self
            .driver
            .skill(crate::role_header::skill_name(&text))
            .ok();
        let prompt = crate::role_header::compose("codex", &text, skill.as_deref());
        let session = self.turn(None, &prompt)?;
        let head = self.driver.head()?;
        if !self.driver.has_commits_beyond(&self.base, &head)? {
            return Err("the writer left no commit beyond the base".into());
        }
        Ok(Write { head, session })
    }

    fn review(&mut self, head: &str, _stamp: &str) -> Result<Review, String> {
        if !self.driver.stamped(head) {
            return Err(format!("review_refused: no fast-checks stamp for {head}"));
        }
        let previous = self.previous();
        let run = self.driver.review(head, previous.as_ref())?;
        if run["head"] != head {
            return Err(format!("the review ran on another head than {head}"));
        }
        let verdict = match run["verdict"].as_str() {
            Some("ACCEPT") => Verdict::Accept,
            Some("REQUEST_CHANGES") => Verdict::RequestChanges,
            other => {
                let why = run["reason"].as_str().or(run["status"].as_str());
                return Err(format!(
                    "no verdict on {head}: {}",
                    why.or(other).unwrap_or("weak_verdict")
                ));
            }
        };
        let id = run["run_id"].as_str().unwrap_or_default();
        let path = self.findings_path(id)?;
        std::fs::create_dir_all(&self.findings).map_err(|err| err.to_string())?;
        let text = run["text"].as_str().unwrap_or_default().to_owned();
        std::fs::write(&path, &text).map_err(|err| format!("review text not kept: {err}"))?;
        match verdict {
            Verdict::RequestChanges => {
                let mut rounds = self.rounds();
                if !rounds.iter().any(|(seen, _)| seen == id) {
                    rounds.push((id.to_owned(), crate::native_writer::high_med(&text)));
                }
                self.keep("rounds.json", &rounds)?;
                self.keep("previous.json", &run)?;
            }
            Verdict::Accept => {
                let _ = std::fs::remove_file(self.state_path("previous.json"));
            }
        }
        Ok(Review {
            verdict,
            review: id.to_owned(),
        })
    }

    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        let text = std::fs::read_to_string(self.findings_path(review)?)
            .map_err(|err| format!("findings of review {review} unreadable: {err}"))?;
        // The shared round stop rule decides before every fix: no progress, slow progress or
        // the cap ends the batch instead of spending another turn.
        let counts: Vec<u64> = self.rounds().iter().map(|(_, count)| *count).collect();
        if let Some(reason) = crate::native_writer::round_stop(&counts) {
            return Err(format!("round_stop: {reason}"));
        }
        let prompt =
            format!("Open findings of the review of {head} on this worktree; fix them:\n{text}\n");
        self.turn(Some(session), &prompt)?;
        let fixed = self.driver.head()?;
        if fixed == head {
            return Err("the fix turn left the head unchanged".into());
        }
        Ok(Fix { head: fixed })
    }

    fn claim(&mut self, _: &[String]) -> Result<Claim, String> {
        Err(not_agent("claim"))
    }
    /// The checkout is the driver's: it pins the base the review reads and admits origin/main.
    fn worktree(&mut self, _claim: &str, base: &str) -> Result<Worktree, String> {
        let path = self.driver.worktree().ok_or("the driver has no worktree")?;
        // A kill between the checkout and its row leaves the checkout: reuse it at the base.
        let at_base =
            path.join(".git").exists() && self.driver.head().is_ok_and(|head| head == base);
        if !at_base {
            let got = self.driver.prepare()?;
            if got != base {
                return Err(format!("the base resolved to {got}, not {base}"));
            }
        }
        Ok(Worktree {
            worktree: path.to_string_lossy().into_owned(),
        })
    }
    /// The driver's gates write the stamp the review seat reads (the worktree's own git
    /// directory), so one receipt serves checks, review and push.
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        if let Some(basis) = self.driver.prepare_review(head)? {
            if basis.head != head {
                return Err(format!(
                    "the package was rebased onto a newer main as {}; start the checks again",
                    basis.head
                ));
            }
        }
        self.driver.fast_checks(head)?;
        let stamp = self.driver.stamp_path(head).filter(|path| path.is_file());
        let stamp = stamp.ok_or_else(|| format!("fast checks left no stamp for {head}"))?;
        Ok(FastChecks {
            stamp: stamp.to_string_lossy().into_owned(),
        })
    }
    fn push(&mut self, _: &str, _: &str, _: &str) -> Result<Push, String> {
        Err(not_agent("push"))
    }
    fn ci_class(&mut self, _: &str, _: u64) -> Result<CiClass, String> {
        Err(not_agent("ci_class"))
    }
    fn train_join(&mut self, _: &str, _: u64, _: u64, _: u32) -> Result<TrainJoin, String> {
        Err(not_agent("train_join"))
    }
}

fn not_agent(step: &str) -> String {
    format!("{step} is not an agent step")
}
