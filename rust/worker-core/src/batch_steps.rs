//! Version 1 of the batch step contract. All references are data supplied by the caller.

use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BatchSteps {
    pub version: u32,
    pub repo: String,
    pub batch_key: String,
    pub rows: Vec<StepAttempt>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StepAttempt {
    pub step: Step,
    pub stage: Stage,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Stage {
    pub step_index: usize,
    pub attempt: u32,
    pub started_at_ms: i64,
    pub ended_at_ms: i64,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum ResultOf<T> {
    Completed { output: T },
    Failed { reason: String },
}

impl<T> ResultOf<T> {
    fn output(&self) -> Result<Option<&T>, &'static str> {
        match self {
            Self::Completed { output } => Ok(Some(output)),
            Self::Failed { reason } if !reason.trim().is_empty() => Ok(None),
            Self::Failed { .. } => Err("failed attempt requires a reason"),
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Step {
    Claim {
        items: Vec<String>,
        result: ResultOf<Claim>,
    },
    Worktree {
        claim: String,
        base: String,
        result: ResultOf<Worktree>,
    },
    Write {
        worktree: String,
        brief: String,
        result: ResultOf<Write>,
    },
    FastChecks {
        head: String,
        result: ResultOf<FastChecks>,
    },
    Review {
        head: String,
        stamp: String,
        result: ResultOf<Review>,
    },
    Fix {
        head: String,
        session: String,
        review: String,
        result: ResultOf<Fix>,
    },
    Push {
        head: String,
        stamp: String,
        review: String,
        result: ResultOf<Push>,
    },
    CiClass {
        head: String,
        pr: u64,
        result: ResultOf<CiClass>,
    },
    TrainJoin {
        head: String,
        pr: u64,
        run: u64,
        attempt: u32,
        result: ResultOf<TrainJoin>,
    },
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Claim {
    pub claim: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Worktree {
    pub worktree: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Write {
    pub head: String,
    pub session: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FastChecks {
    pub stamp: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Review {
    pub verdict: Verdict,
    pub review: String,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Accept,
    RequestChanges,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fix {
    pub head: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Push {
    pub pr: u64,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CiClass {
    pub classification: Classification,
    pub run: u64,
    pub attempt: u32,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Classification {
    Green,
    Red,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrainJoin {
    pub train: String,
    pub manifest: String,
}

enum Next<'a> {
    Claim,
    Worktree(&'a str),
    Write(&'a str),
    FastChecks(&'a str, &'a str),
    Review(&'a str, &'a str, &'a str),
    Fix(&'a str, &'a str, &'a str),
    Push(&'a str, &'a str, &'a str),
    CiClass(&'a str, u64),
    TrainJoin(&'a str, u64, u64, u32),
    Joined,
    Red,
}

fn present(value: &str) -> bool {
    !value.trim().is_empty()
}

fn git_id(value: &str) -> bool {
    matches!(value.len(), 40 | 64) && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

impl<'a> Next<'a> {
    fn advance(&self, step: &'a Step) -> Result<Option<Self>, &'static str> {
        use Next::*;
        let next = match (self, step) {
            (Claim, Step::Claim { items, result })
                if !items.is_empty() && items.iter().all(|item| present(item)) =>
            {
                result
                    .output()?
                    .and_then(|out| present(&out.claim).then_some(Worktree(&out.claim)))
            }
            (
                Worktree(id),
                Step::Worktree {
                    claim,
                    base,
                    result,
                },
            ) if *id == claim && git_id(base) => result
                .output()?
                .and_then(|out| present(&out.worktree).then_some(Write(&out.worktree))),
            (
                Write(id),
                Step::Write {
                    worktree,
                    brief,
                    result,
                },
            ) if *id == worktree && present(brief) => result.output()?.and_then(|out| {
                (git_id(&out.head) && present(&out.session))
                    .then_some(FastChecks(&out.head, &out.session))
            }),
            (FastChecks(expected, session), Step::FastChecks { head, result })
                if *expected == head =>
            {
                result.output()?.and_then(|out| {
                    present(&out.stamp).then_some(Review(expected, session, &out.stamp))
                })
            }
            (
                Review(expected, session, checked),
                Step::Review {
                    head,
                    stamp,
                    result,
                },
            ) if *expected == head && *checked == stamp => result.output()?.and_then(|out| {
                present(&out.review).then_some(match out.verdict {
                    Verdict::Accept => Push(expected, checked, &out.review),
                    Verdict::RequestChanges => Fix(expected, session, &out.review),
                })
            }),
            (
                Fix(expected, writer, finding),
                Step::Fix {
                    head,
                    session,
                    review,
                    result,
                },
            ) if *expected == head && *writer == session && *finding == review => {
                result.output()?.and_then(|out| {
                    (git_id(&out.head) && out.head != head.as_str())
                        .then_some(FastChecks(&out.head, writer))
                })
            }
            (
                Push(expected, checked, accepted),
                Step::Push {
                    head,
                    stamp,
                    review,
                    result,
                },
            ) if *expected == head && *checked == stamp && *accepted == review => result
                .output()?
                .and_then(|out| (out.pr > 0).then_some(CiClass(expected, out.pr))),
            (CiClass(expected, pull), Step::CiClass { head, pr, result })
                if *expected == head && pull == pr =>
            {
                result.output()?.and_then(|out| {
                    (out.run > 0 && out.attempt > 0).then_some(match out.classification {
                        Classification::Green => TrainJoin(expected, *pr, out.run, out.attempt),
                        Classification::Red => Red,
                    })
                })
            }
            (
                TrainJoin(expected, pull, ci_run, ci_attempt),
                Step::TrainJoin {
                    head,
                    pr,
                    run,
                    attempt,
                    result,
                },
            ) if *expected == head && pull == pr && ci_run == run && ci_attempt == attempt => {
                result.output()?.and_then(|out| {
                    (present(&out.train) && present(&out.manifest)).then_some(Joined)
                })
            }
            _ => return Err("step out of order or input does not match"),
        };
        if next.is_none() && !step.failed() {
            return Err("invalid step output");
        }
        Ok(next)
    }
}

impl Step {
    fn failed(&self) -> bool {
        match self {
            Self::Claim { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::Worktree { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::Write { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::FastChecks { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::Review { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::Fix { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::Push { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::CiClass { result, .. } => matches!(result, ResultOf::Failed { .. }),
            Self::TrainJoin { result, .. } => matches!(result, ResultOf::Failed { .. }),
        }
    }
}

impl BatchSteps {
    /// Validate a recorded prefix, including the order and evidence bindings of successful steps.
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 || !present(&self.repo) || !present(&self.batch_key) {
            return Err("invalid batch version, repo or key".into());
        }
        let mut next = Next::Claim;
        let (mut step_index, mut attempt) = (0, 1);
        let mut previous_end = i64::MIN;
        for row in &self.rows {
            let stage = &row.stage;
            if stage.step_index != step_index
                || stage.attempt != attempt
                || stage.started_at_ms < previous_end
                || stage.ended_at_ms < stage.started_at_ms
            {
                return Err(format!(
                    "invalid stage row for step {step_index} attempt {attempt}"
                ));
            }
            match next
                .advance(&row.step)
                .map_err(|err| format!("step {step_index}: {err}"))?
            {
                Some(following) => {
                    next = following;
                    step_index += 1;
                    attempt = 1;
                }
                None => {
                    attempt = attempt.checked_add(1).ok_or("stage attempt overflow")?;
                }
            }
            previous_end = stage.ended_at_ms;
        }
        Ok(())
    }
}
