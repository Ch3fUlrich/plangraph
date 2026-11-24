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
    /// The last REQUEST_CHANGES review, for the delta review of the next round.
    previous: Option<Value>,
    /// A usage limit or a stop ends the batch at once: another turn only spends the quota.
    halted: Option<String>,
}

impl<D: Driver> AgentSteps<D> {
    pub(crate) fn new(driver: D, base: String, findings: PathBuf) -> Self {
        Self {
            driver,
            base,
            findings,
            previous: None,
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
        let run = self.driver.review(head, self.previous.as_ref())?;
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
        std::fs::write(&path, run["text"].as_str().unwrap_or_default())
            .map_err(|err| format!("review text not kept: {err}"))?;
        self.previous = matches!(verdict, Verdict::RequestChanges).then_some(run.clone());
        Ok(Review {
            verdict,
            review: id.to_owned(),
        })
    }

    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        let text = std::fs::read_to_string(self.findings_path(review)?)
            .map_err(|err| format!("findings of review {review} unreadable: {err}"))?;
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
    fn worktree(&mut self, _: &str, _: &str) -> Result<Worktree, String> {
        Err(not_agent("worktree"))
    }
    fn fast_checks(&mut self, _: &str) -> Result<FastChecks, String> {
        Err(not_agent("fast_checks"))
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
