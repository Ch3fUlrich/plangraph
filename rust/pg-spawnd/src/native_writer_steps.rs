//! The agent steps of the batch step list (write, review, fix) over the native writer
//! `Driver`: write is the first writer turn, review is one review run on the exact head, fix
//! is a turn of the same writer session. The step runner owns the order, the attempts and
//! the round cap; this adapter owns only what a turn needs, so the loop of `native_writer`
//! and the runner do not both drive a batch.

use crate::native_writer::{quota, Driver};
use crate::writer_control::STOPPED;
use pg_spawnd::step_runner::StepActions;
use pg_worker_core::batch_steps::{
    BatchSteps, CiClass, Claim, FastChecks, Fix, Push, ResultOf, Review, Step, TrainJoin, Verdict,
    Worktree, Write,
};
use serde_json::Value;
use std::path::PathBuf;

pub(crate) struct AgentSteps<D: Driver> {
    pub driver: D,
    /// The base the batch was cut from: a writer turn must commit beyond it.
    pub base: String,
    /// Where every review run is kept, one file per run, so a fix turn of a resumed list
    /// still reads the findings its `review` reference names.
    pub findings: PathBuf,
    /// The steps file: the committed rows are the only record of which reviews count. A review
    /// run kept in `findings` whose row was never saved (a kill in between) is not counted.
    pub steps: PathBuf,
    /// A usage limit or a stop ends the batch at once: another turn only spends the quota.
    halted: Option<String>,
}

impl<D: Driver> AgentSteps<D> {
    pub(crate) fn new(driver: D, base: String, findings: PathBuf, steps: PathBuf) -> Self {
        Self {
            driver,
            base,
            findings,
            steps,
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

    /// The reviews of the committed rows, oldest first, each with its kept run. The runner
    /// saves a row only after its action returned, so a review whose row is missing never
    /// counted toward the stop rule and never became the previous review of a delta.
    fn committed_reviews(&self) -> Vec<(Verdict, Value)> {
        let Ok(bytes) = std::fs::read(&self.steps) else {
            return Vec::new();
        };
        let Ok(steps) = serde_json::from_slice::<BatchSteps>(&bytes) else {
            return Vec::new();
        };
        steps
            .rows
            .iter()
            .filter_map(|row| match &row.step {
                Step::Review {
                    result: ResultOf::Completed { output },
                    ..
                } => Some((output.verdict, self.kept(&output.review)?)),
                _ => None,
            })
            .collect()
    }

    fn kept(&self, run: &str) -> Option<Value> {
        let text = std::fs::read_to_string(self.findings_path(run).ok()?).ok()?;
        serde_json::from_str(&text).ok()
    }

    /// The review the next one is a delta of: the last committed one, when it asked for changes.
    fn previous(&self) -> Option<Value> {
        let (verdict, run) = self.committed_reviews().pop()?;
        matches!(verdict, Verdict::RequestChanges).then_some(run)
    }

    /// Save what the review admission reads from the writer's record (`base_fresh`): the
    /// admitted base, the review basis, the exact head and its passed checks, and the previous
    /// review a delta review starts from.
    fn publish_review_state(
        &mut self,
        head: &str,
        basis: &crate::native_writer::ReviewBase,
    ) -> Result<(), String> {
        let mut record = self.driver.load()?;
        record["base_admitted"] = serde_json::json!(true);
        record["base_sha"] = serde_json::json!(basis.base);
        record["review_base"] = serde_json::json!(basis);
        record["final_head"] = serde_json::json!(head);
        record["fast_checks"] = serde_json::json!({"head": head, "status": "passed"});
        match self.previous() {
            Some(previous) => record["review"] = previous,
            None => {
                record.as_object_mut().map(|fields| fields.remove("review"));
            }
        }
        self.driver.save(&record)
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
        Ok(self.findings.join(format!("{run}.json")))
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
        std::fs::write(&path, run.to_string())
            .map_err(|err| format!("review run not kept: {err}"))?;
        Ok(Review {
            verdict,
            review: id.to_owned(),
        })
    }

    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        let rounds: Vec<Value> = self
            .committed_reviews()
            .into_iter()
            .filter(|(verdict, _)| matches!(verdict, Verdict::RequestChanges))
            .map(|(_, run)| run)
            .collect();
        let Some(this) = rounds.iter().find(|run| run["run_id"] == review) else {
            return Err(format!("review {review} is not a recorded review"));
        };
        let text = this["text"].as_str().unwrap_or_default().to_owned();
        // The shared round stop rule decides before every fix: no progress, slow progress or
        // the cap ends the batch instead of spending another turn.
        let counts: Vec<u64> = rounds
            .iter()
            .map(|run| crate::native_writer::high_med(run["text"].as_str().unwrap_or_default()))
            .collect();
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
        // The gates read files: they speak for `head` only when the tree is the committed one.
        self.driver.clean()?;
        // A main that moved is refused here, before the checkout changes: the step list binds
        // every later step to this head, so a rebase underneath it would strand them.
        let basis = self.driver.probe_review_base(head)?;
        self.driver.fast_checks(head)?;
        let stamp = self.driver.stamp_path(head).filter(|path| path.is_file());
        let stamp = stamp.ok_or_else(|| format!("fast checks left no stamp for {head}"))?;
        if let Some(basis) = basis {
            self.publish_review_state(head, &basis)?;
        }
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
