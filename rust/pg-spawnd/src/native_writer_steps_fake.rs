//! The driver fake and the helpers of the agent step tests.
use crate::native_writer::{quota, Driver};
use crate::native_writer_steps::AgentSteps;
use pg_spawnd::step_runner::*;

use pg_worker_core::batch_steps::*;
use serde_json::{json, Value};

pub(crate) const BASE: &str = "0000000000000000000000000000000000000000";

pub(crate) fn sha(digit: char) -> String {
    digit.to_string().repeat(40)
}

/// A driver whose writer commits a new head per turn and whose reviews follow a script.
#[derive(Default)]
pub(crate) struct Fake {
    /// `(session, prompt)` of every writer turn.
    pub(crate) turns: Vec<(Option<String>, String)>,
    pub(crate) heads: Vec<String>,
    pub(crate) verdicts: Vec<&'static str>,
    pub(crate) reviews: Vec<(String, Option<Value>)>,
    pub(crate) stamped: Vec<String>,
    pub(crate) booked: usize,
    pub(crate) execute_error: Option<String>,
    /// Writer turns that leave HEAD where it was.
    pub(crate) no_commit: bool,
    /// Where stamps are written; fast checks of a fake without one leave none.
    pub(crate) stamp_dir: Option<std::path::PathBuf>,
    pub(crate) prepared: usize,
    pub(crate) dirty: bool,
    /// The driver reports a review basis; with `main_moved` it refuses instead.
    pub(crate) probe: bool,
    pub(crate) main_moved: bool,
    pub(crate) record: Value,
    pub(crate) saved: Vec<Value>,
    /// HIGH finding lines per review text.
    pub(crate) findings: usize,
    /// The base `prepare` resolves, when not BASE.
    pub(crate) moved_base: Option<String>,
}

impl Driver for Fake {
    fn prepare(&mut self) -> Result<String, String> {
        self.prepared += 1;
        Ok(self.moved_base.clone().unwrap_or_else(|| BASE.into()))
    }
    fn worktree(&self) -> Option<std::path::PathBuf> {
        Some("/nonexistent/worktree".into())
    }
    fn stamp_path(&mut self, head: &str) -> Option<std::path::PathBuf> {
        self.stamp_dir.as_ref().map(|dir| dir.join(head))
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        unreachable!()
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        Ok(100)
    }
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        self.turns.push((session.map(str::to_owned), prompt.into()));
        if let Some(error) = &self.execute_error {
            return Err(error.clone());
        }
        if !self.no_commit {
            self.heads
                .push(sha(char::from(b'0' + self.turns.len() as u8)));
        }
        Ok("writer-session".into())
    }
    fn head(&mut self) -> Result<String, String> {
        Ok(self.heads.last().cloned().unwrap_or_else(|| BASE.into()))
    }
    fn has_commits_beyond(&mut self, base: &str, head: &str) -> Result<bool, String> {
        Ok(base != head)
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        self.stamped.push(head.into());
        if let Some(dir) = &self.stamp_dir {
            std::fs::write(dir.join(head), "green\n").map_err(|err| err.to_string())?;
        }
        Ok(())
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.stamped.iter().any(|stamped| stamped == head)
    }
    fn review(&mut self, head: &str, previous: Option<&Value>) -> Result<Value, String> {
        self.reviews.push((head.into(), previous.cloned()));
        let verdict = if self.verdicts.is_empty() {
            "ACCEPT"
        } else {
            self.verdicts.remove(0)
        };
        Ok(
            json!({"verdict":verdict, "head":head, "run_id":format!("run-r{}", self.reviews.len()),
            "text":format!("{}VERDICT: {verdict}\n", "HIGH a.rs:1 - finding\n".repeat(self.findings.max(1)))}),
        )
    }
    fn gates(&mut self) -> Result<(), String> {
        Ok(())
    }
    fn record_quota(&mut self, _: &quota::Limit) -> Result<(), String> {
        self.booked += 1;
        Ok(())
    }
    fn save(&mut self, record: &Value) -> Result<(), String> {
        self.saved.push(record.clone());
        Ok(())
    }
    fn clean(&mut self) -> Result<(), String> {
        if self.dirty {
            return Err("the worktree differs from the committed tree: tools/gate.py".into());
        }
        Ok(())
    }
    fn probe_review_base(
        &mut self,
        head: &str,
    ) -> Result<Option<crate::native_writer::ReviewBase>, String> {
        match (self.probe, self.main_moved) {
            (false, _) => Ok(None),
            (true, true) => Err("origin/main moved since the base was pinned".into()),
            (true, false) => Ok(Some(crate::native_writer::ReviewBase {
                head: head.into(),
                base: BASE.into(),
                main: sha('5'),
                observed_main: sha('5'),
                mode: "current",
            })),
        }
    }
    fn load(&mut self) -> Result<Value, String> {
        Ok(self.record.clone())
    }
}

/// What the runner does after a review action returns: save the row.
pub(crate) fn commit(steps_path: &std::path::Path, head: &str, review: &Review) {
    let mut list: BatchSteps = std::fs::read(steps_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or(BatchSteps {
            version: 1,
            repo: "demo/repo".into(),
            batch_key: "b".into(),
            rows: Vec::new(),
        });
    let at = list.rows.len() as i64;
    list.rows.push(StepAttempt {
        step: Step::Review {
            head: head.into(),
            stamp: "stamp".into(),
            result: ResultOf::Completed {
                output: Review {
                    verdict: review.verdict,
                    review: review.review.clone(),
                },
            },
        },
        stage: Stage {
            step_index: list.rows.len(),
            attempt: 1,
            started_at_ms: at,
            ended_at_ms: at,
        },
    });
    std::fs::write(steps_path, serde_json::to_vec(&list).unwrap()).unwrap();
}

pub(crate) fn agents(fake: Fake, dir: &std::path::Path) -> AgentSteps<Fake> {
    AgentSteps::new(
        fake,
        BASE.into(),
        dir.join("findings"),
        dir.join("steps.json"),
    )
}

pub(crate) fn brief(dir: &std::path::Path) -> String {
    let path = dir.join("brief.md");
    std::fs::write(&path, "# batch\nitem text\n").unwrap();
    path.to_string_lossy().into_owned()
}

/// A review round that asks for changes with `count` HIGH findings.
pub(crate) fn changes(steps: &mut AgentSteps<Fake>, head: &str, count: usize) -> Review {
    steps.driver.stamped.push(head.into());
    steps.driver.verdicts = vec!["REQUEST_CHANGES"];
    steps.driver.findings = count;
    let review = steps.review(head, "stamp").unwrap();
    commit(&steps.steps, head, &review);
    review
}

pub(crate) fn stamping(dir: &std::path::Path, fake: Fake) -> AgentSteps<Fake> {
    agents(
        Fake {
            stamp_dir: Some(dir.to_owned()),
            ..fake
        },
        dir,
    )
}
