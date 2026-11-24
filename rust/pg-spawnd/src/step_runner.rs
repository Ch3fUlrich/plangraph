//! Runs the per-batch step list of the batch step contract (`pg_worker_core::batch_steps`).
//!
//! The runner owns the order: claim, worktree, write, then fast checks and review with fix
//! rounds until a review accepts, then push, CI class and train join. Every attempt of every
//! step becomes one contract row (`StepAttempt`) that is validated by the contract itself
//! before it is recorded, so a stage row exists per step and the list can be resumed.
//! What a step does is behind `StepActions`; deterministic and agent steps use one seam.

use pg_worker_core::batch_steps::{
    BatchSteps, CiClass, Claim, Classification, FastChecks, Fix, Push, ResultOf, Review, Stage,
    Step, StepAttempt, TrainJoin, Verdict, Worktree, Write,
};

/// One method per step type. An `Err` is a failed attempt and carries its reason.
pub trait StepActions {
    fn claim(&mut self, items: &[String]) -> Result<Claim, String>;
    fn worktree(&mut self, claim: &str, base: &str) -> Result<Worktree, String>;
    fn write(&mut self, worktree: &str, brief: &str) -> Result<Write, String>;
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String>;
    fn review(&mut self, head: &str, stamp: &str) -> Result<Review, String>;
    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String>;
    fn push(&mut self, head: &str, stamp: &str, review: &str) -> Result<Push, String>;
    fn ci_class(&mut self, head: &str, pr: u64) -> Result<CiClass, String>;
    fn train_join(
        &mut self,
        head: &str,
        pr: u64,
        run: u64,
        attempt: u32,
    ) -> Result<TrainJoin, String>;
}

/// Receives the whole list after every recorded attempt; the last row is the new one.
pub trait StageSink {
    fn record(&mut self, steps: &BatchSteps);
}

/// What the batch starts from. All of it is data from the caller.
#[derive(Clone, Debug)]
pub struct Inputs {
    pub repo: String,
    pub batch_key: String,
    pub items: Vec<String>,
    pub base: String,
    pub brief: String,
}

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Attempts of one step before the batch stops.
    pub attempts: u32,
    /// Review rounds that may end in a fix (the writer round cap).
    pub fix_rounds: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            attempts: 2,
            fix_rounds: 8,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    Joined {
        train: String,
        manifest: String,
    },
    /// CI classified the head red; the autofix item decides what follows.
    CiRed {
        head: String,
        pr: u64,
    },
    Failed {
        step: &'static str,
        step_index: usize,
        reason: String,
    },
    FixRoundsExhausted {
        rounds: u32,
    },
    /// An action returned output the contract refuses; nothing was recorded for it.
    Invalid {
        step: &'static str,
        step_index: usize,
        reason: String,
    },
}

#[derive(Debug)]
enum State {
    Claim,
    Worktree {
        claim: String,
    },
    Write {
        worktree: String,
    },
    FastChecks {
        head: String,
        session: String,
    },
    Review {
        head: String,
        session: String,
        stamp: String,
    },
    Fix {
        head: String,
        session: String,
        review: String,
    },
    Push {
        head: String,
        stamp: String,
        review: String,
    },
    CiClass {
        head: String,
        pr: u64,
    },
    TrainJoin {
        head: String,
        pr: u64,
        run: u64,
        attempt: u32,
    },
    Joined {
        train: String,
        manifest: String,
    },
    Red {
        head: String,
        pr: u64,
    },
}

impl State {
    /// The state after a recorded step, or `None` when that attempt did not complete.
    fn after(&self, step: &Step) -> Option<State> {
        use ResultOf::Completed;
        Some(match (self, step) {
            (
                State::Claim,
                Step::Claim {
                    result: Completed { output },
                    ..
                },
            ) => State::Worktree {
                claim: output.claim.clone(),
            },
            (
                State::Worktree { .. },
                Step::Worktree {
                    result: Completed { output },
                    ..
                },
            ) => State::Write {
                worktree: output.worktree.clone(),
            },
            (
                State::Write { .. },
                Step::Write {
                    result: Completed { output },
                    ..
                },
            ) => State::FastChecks {
                head: output.head.clone(),
                session: output.session.clone(),
            },
            (
                State::FastChecks { session, .. },
                Step::FastChecks {
                    head,
                    result: Completed { output },
                },
            ) => State::Review {
                head: head.clone(),
                session: session.clone(),
                stamp: output.stamp.clone(),
            },
            (
                State::Review { session, .. },
                Step::Review {
                    head,
                    stamp,
                    result: Completed { output },
                },
            ) => match output.verdict {
                Verdict::Accept => State::Push {
                    head: head.clone(),
                    stamp: stamp.clone(),
                    review: output.review.clone(),
                },
                Verdict::RequestChanges => State::Fix {
                    head: head.clone(),
                    session: session.clone(),
                    review: output.review.clone(),
                },
            },
            (
                State::Fix { session, .. },
                Step::Fix {
                    result: Completed { output },
                    ..
                },
            ) => State::FastChecks {
                head: output.head.clone(),
                session: session.clone(),
            },
            (
                State::Push { .. },
                Step::Push {
                    head,
                    result: Completed { output },
                    ..
                },
            ) => State::CiClass {
                head: head.clone(),
                pr: output.pr,
            },
            (
                State::CiClass { .. },
                Step::CiClass {
                    head,
                    pr,
                    result: Completed { output },
                },
            ) => match output.classification {
                Classification::Green => State::TrainJoin {
                    head: head.clone(),
                    pr: *pr,
                    run: output.run,
                    attempt: output.attempt,
                },
                Classification::Red => State::Red {
                    head: head.clone(),
                    pr: *pr,
                },
            },
            (
                State::TrainJoin { .. },
                Step::TrainJoin {
                    result: Completed { output },
                    ..
                },
            ) => State::Joined {
                train: output.train.clone(),
                manifest: output.manifest.clone(),
            },
            _ => return None,
        })
    }
}

/// Wraps an action result; the contract refuses a failed attempt without a reason.
fn wrap<T>(result: Result<T, String>) -> ResultOf<T> {
    match result {
        Ok(output) => ResultOf::Completed { output },
        Err(reason) if reason.trim().is_empty() => ResultOf::Failed {
            reason: "step failed without a reason".into(),
        },
        Err(reason) => ResultOf::Failed { reason },
    }
}

/// The step type name, also the key of its stage row.
pub fn step_name(step: &Step) -> &'static str {
    match step {
        Step::Claim { .. } => "claim",
        Step::Worktree { .. } => "worktree",
        Step::Write { .. } => "write",
        Step::FastChecks { .. } => "fast_checks",
        Step::Review { .. } => "review",
        Step::Fix { .. } => "fix",
        Step::Push { .. } => "push",
        Step::CiClass { .. } => "ci_class",
        Step::TrainJoin { .. } => "train_join",
    }
}

fn failure(step: &Step) -> Option<&str> {
    macro_rules! failed {
        ($result:expr) => {
            match $result {
                ResultOf::Failed { reason } => Some(reason.as_str()),
                ResultOf::Completed { .. } => None,
            }
        };
    }
    match step {
        Step::Claim { result, .. } => failed!(result),
        Step::Worktree { result, .. } => failed!(result),
        Step::Write { result, .. } => failed!(result),
        Step::FastChecks { result, .. } => failed!(result),
        Step::Review { result, .. } => failed!(result),
        Step::Fix { result, .. } => failed!(result),
        Step::Push { result, .. } => failed!(result),
        Step::CiClass { result, .. } => failed!(result),
        Step::TrainJoin { result, .. } => failed!(result),
    }
}

/// Where the list stands: the state, and the step index and attempt the next row gets.
struct Progress {
    state: State,
    index: usize,
    attempt: u32,
    fix_rounds: u32,
}

impl Progress {
    /// Move on after a recorded step, or count one more attempt; true when it moved on.
    fn advance(&mut self, step: &Step) -> bool {
        let Some(next) = self.state.after(step) else {
            self.attempt += 1;
            return false;
        };
        if matches!(next, State::Fix { .. }) {
            self.fix_rounds += 1;
        }
        self.state = next;
        self.index += 1;
        self.attempt = 1;
        true
    }
}

pub struct Runner {
    inputs: Inputs,
    limits: Limits,
    steps: BatchSteps,
    progress: Progress,
    last_end: i64,
}

impl Runner {
    pub fn new(inputs: Inputs, limits: Limits) -> Self {
        let steps = BatchSteps {
            version: 1,
            repo: inputs.repo.clone(),
            batch_key: inputs.batch_key.clone(),
            rows: Vec::new(),
        };
        Self::resume(inputs, limits, steps).expect("an empty step list is valid")
    }

    /// Continue from a recorded prefix. The contract validates it and the same transition
    /// function replays it, so a resumed batch is in the state a live one would be in.
    pub fn resume(inputs: Inputs, limits: Limits, steps: BatchSteps) -> Result<Self, String> {
        steps.validate()?;
        if steps.repo != inputs.repo || steps.batch_key != inputs.batch_key {
            return Err("recorded steps belong to another repo or batch".into());
        }
        let mut progress = Progress {
            state: State::Claim,
            index: 0,
            attempt: 1,
            fix_rounds: 0,
        };
        let mut last_end = i64::MIN;
        for row in &steps.rows {
            last_end = row.stage.ended_at_ms;
            progress.advance(&row.step);
        }
        Ok(Self {
            inputs,
            limits,
            steps,
            progress,
            last_end,
        })
    }

    pub fn steps(&self) -> &BatchSteps {
        &self.steps
    }

    fn terminal(&self) -> Option<Outcome> {
        match &self.progress.state {
            State::Joined { train, manifest } => Some(Outcome::Joined {
                train: train.clone(),
                manifest: manifest.clone(),
            }),
            State::Red { head, pr } => Some(Outcome::CiRed {
                head: head.clone(),
                pr: *pr,
            }),
            State::Fix { .. } if self.progress.fix_rounds > self.limits.fix_rounds => {
                Some(Outcome::FixRoundsExhausted {
                    rounds: self.limits.fix_rounds,
                })
            }
            _ => None,
        }
    }

    /// Run the current step once and return it, inputs taken from the current state.
    fn attempt_step(&self, actions: &mut dyn StepActions) -> Step {
        let inputs = &self.inputs;
        match &self.progress.state {
            State::Claim => Step::Claim {
                items: inputs.items.clone(),
                result: wrap(actions.claim(&inputs.items)),
            },
            State::Worktree { claim } => Step::Worktree {
                claim: claim.clone(),
                base: inputs.base.clone(),
                result: wrap(actions.worktree(claim, &inputs.base)),
            },
            State::Write { worktree } => Step::Write {
                worktree: worktree.clone(),
                brief: inputs.brief.clone(),
                result: wrap(actions.write(worktree, &inputs.brief)),
            },
            State::FastChecks { head, .. } => Step::FastChecks {
                head: head.clone(),
                result: wrap(actions.fast_checks(head)),
            },
            State::Review { head, stamp, .. } => Step::Review {
                head: head.clone(),
                stamp: stamp.clone(),
                result: wrap(actions.review(head, stamp)),
            },
            State::Fix {
                head,
                session,
                review,
            } => Step::Fix {
                head: head.clone(),
                session: session.clone(),
                review: review.clone(),
                result: wrap(actions.fix(head, session, review)),
            },
            State::Push {
                head,
                stamp,
                review,
            } => Step::Push {
                head: head.clone(),
                stamp: stamp.clone(),
                review: review.clone(),
                result: wrap(actions.push(head, stamp, review)),
            },
            State::CiClass { head, pr } => Step::CiClass {
                head: head.clone(),
                pr: *pr,
                result: wrap(actions.ci_class(head, *pr)),
            },
            State::TrainJoin {
                head,
                pr,
                run,
                attempt,
            } => Step::TrainJoin {
                head: head.clone(),
                pr: *pr,
                run: *run,
                attempt: *attempt,
                result: wrap(actions.train_join(head, *pr, *run, *attempt)),
            },
            State::Joined { .. } | State::Red { .. } => unreachable!("terminal state"),
        }
    }

    /// Run the list to its end: a joined train, a red CI, or the step that stopped it.
    pub fn run(
        &mut self,
        actions: &mut dyn StepActions,
        sink: &mut dyn StageSink,
        clock: &mut dyn FnMut() -> i64,
    ) -> Outcome {
        loop {
            if let Some(outcome) = self.terminal() {
                return outcome;
            }
            let started = clock().max(self.last_end);
            let step = self.attempt_step(actions);
            let ended = clock().max(started);
            let (index, attempt) = (self.progress.index, self.progress.attempt);
            let row = StepAttempt {
                step,
                stage: Stage {
                    step_index: index,
                    attempt,
                    started_at_ms: started,
                    ended_at_ms: ended,
                },
            };
            let name = step_name(&row.step);
            self.steps.rows.push(row);
            if let Err(reason) = self.steps.validate() {
                self.steps.rows.pop();
                return Outcome::Invalid {
                    step: name,
                    step_index: index,
                    reason,
                };
            }
            sink.record(&self.steps);
            self.last_end = ended;
            let step = &self.steps.rows.last().expect("row just pushed").step;
            let reason = failure(step).unwrap_or_default().to_owned();
            if !self.progress.advance(step) && self.progress.attempt > self.limits.attempts {
                return Outcome::Failed {
                    step: name,
                    step_index: index,
                    reason,
                };
            }
        }
    }
}
