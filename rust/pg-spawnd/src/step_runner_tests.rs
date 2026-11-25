use crate::step_runner::*;
use crate::step_runner_log::{journal_row, StepLog};
use crate::step_runner_shell::*;
use pg_worker_core::batch_steps::*;
use std::{collections::VecDeque, path::Path};

const BASE: &str = "0000000000000000000000000000000000000000";
const HEAD: &str = "1111111111111111111111111111111111111111";
const FIXED: &str = "2222222222222222222222222222222222222222";

fn inputs() -> Inputs {
    Inputs {
        repo: "demo/repo".into(),
        batch_key: "batch-1".into(),
        items: vec!["demo:ITEM".into()],
        base: BASE.into(),
        brief: "brief-1".into(),
    }
}

/// Scripted actions: `fail` holds step names whose next attempts fail, `verdicts` the reviews.
#[derive(Default)]
struct Script {
    calls: Vec<&'static str>,
    fail: VecDeque<(&'static str, &'static str)>,
    verdicts: VecDeque<Verdict>,
    green: bool,
    stamp: &'static str,
    fixes: u8,
}

impl Script {
    fn new(verdicts: &[Verdict]) -> Self {
        Self {
            verdicts: verdicts.iter().copied().collect(),
            green: true,
            stamp: "stamp",
            ..Self::default()
        }
    }
    fn enter(&mut self, name: &'static str) -> Result<(), String> {
        self.calls.push(name);
        match self.fail.front() {
            Some((step, reason)) if *step == name => {
                let reason = reason.to_string();
                self.fail.pop_front();
                Err(reason)
            }
            _ => Ok(()),
        }
    }
}

impl StepActions for Script {
    fn claim(&mut self, _: &[String]) -> Result<Claim, String> {
        self.enter("claim")?;
        Ok(Claim {
            claim: "claim-1".into(),
        })
    }
    fn worktree(&mut self, _: &str, _: &str) -> Result<Worktree, String> {
        self.enter("worktree")?;
        Ok(Worktree {
            worktree: "wt-1".into(),
        })
    }
    fn write(&mut self, _: &str, _: &str) -> Result<Write, String> {
        self.enter("write")?;
        Ok(Write {
            head: HEAD.into(),
            session: "session-1".into(),
        })
    }
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        self.enter("fast_checks")?;
        Ok(FastChecks {
            stamp: format!("{}-{}", self.stamp, &head[..2]),
        })
    }
    fn review(&mut self, head: &str, _: &str) -> Result<Review, String> {
        self.enter("review")?;
        let verdict = self.verdicts.pop_front().unwrap_or(Verdict::Accept);
        Ok(Review {
            verdict,
            review: format!("review-{}", &head[..2]),
        })
    }
    fn fix(&mut self, _: &str, _: &str, _: &str) -> Result<Fix, String> {
        self.enter("fix")?;
        self.fixes += 1;
        // Every fix round commits a new head.
        Ok(Fix {
            head: char::from(b'1' + self.fixes).to_string().repeat(40),
        })
    }
    fn push(&mut self, _: &str, _: &str, _: &str) -> Result<Push, String> {
        self.enter("push")?;
        Ok(Push { pr: 7 })
    }
    fn ci_class(&mut self, _: &str, _: u64) -> Result<CiClass, String> {
        self.enter("ci_class")?;
        let classification = if self.green {
            Classification::Green
        } else {
            Classification::Red
        };
        Ok(CiClass {
            classification,
            run: 9,
            attempt: 1,
        })
    }
    fn train_join(&mut self, _: &str, _: u64, _: u64, _: u32) -> Result<TrainJoin, String> {
        self.enter("train_join")?;
        Ok(TrainJoin {
            train: "train-1".into(),
            manifest: "manifest-1".into(),
        })
    }
}

#[derive(Default)]
struct Rows(Vec<usize>);

impl StageSink for Rows {
    fn record(&mut self, steps: &BatchSteps) -> Result<(), String> {
        self.0.push(steps.rows.len());
        Ok(())
    }
}

fn tick() -> impl FnMut() -> i64 {
    let mut now = 1000;
    move || {
        now += 10;
        now
    }
}

fn names(runner: &Runner) -> Vec<&'static str> {
    runner
        .steps()
        .rows
        .iter()
        .map(|row| step_name(&row.step))
        .collect()
}

#[test]
fn one_list_runs_from_claim_to_train_join_with_a_stage_row_per_step() {
    let mut actions = Script::new(&[Verdict::RequestChanges, Verdict::Accept]);
    let (mut runner, mut sink) = (Runner::new(inputs(), Limits::default()), Rows::default());
    let outcome = runner.run(&mut actions, &mut sink, &mut tick());
    assert_eq!(
        outcome,
        Outcome::Joined {
            train: "train-1".into(),
            manifest: "manifest-1".into()
        }
    );
    assert_eq!(
        names(&runner),
        [
            "claim",
            "worktree",
            "write",
            "fast_checks",
            "review",
            "fix",
            "fast_checks",
            "review",
            "push",
            "ci_class",
            "train_join"
        ]
    );
    assert_eq!(
        sink.0,
        (1..=11).collect::<Vec<_>>(),
        "one sink call per step, in order"
    );
    let stages: Vec<_> = runner
        .steps()
        .rows
        .iter()
        .map(|row| (row.stage.step_index, row.stage.attempt))
        .collect();
    assert_eq!(stages, (0..11).map(|index| (index, 1)).collect::<Vec<_>>());
    runner.steps().validate().unwrap();
}

#[test]
fn a_failed_attempt_is_a_row_and_the_step_is_retried() {
    let mut actions = Script::new(&[]);
    actions.fail.push_back(("worktree", "disk full"));
    let mut runner = Runner::new(inputs(), Limits::default());
    runner.run(&mut actions, &mut Rows::default(), &mut tick());
    let rows = &runner.steps().rows;
    assert_eq!((rows[1].stage.step_index, rows[1].stage.attempt), (1, 1));
    assert!(
        matches!(&rows[1].step, Step::Worktree { result: ResultOf::Failed { reason }, .. } if reason == "disk full")
    );
    assert_eq!((rows[2].stage.step_index, rows[2].stage.attempt), (1, 2));
}

#[test]
fn a_step_that_keeps_failing_stops_the_batch_with_its_reason() {
    let mut actions = Script::new(&[]);
    actions
        .fail
        .extend([("fast_checks", "clippy"), ("fast_checks", "")]);
    let mut runner = Runner::new(inputs(), Limits::default());
    let outcome = runner.run(&mut actions, &mut Rows::default(), &mut tick());
    assert_eq!(
        outcome,
        Outcome::Failed {
            step: "fast_checks",
            step_index: 3,
            reason: "step failed without a reason".into()
        }
    );
    assert_eq!(
        actions
            .calls
            .iter()
            .filter(|call| **call == "fast_checks")
            .count(),
        2
    );
}

#[test]
fn a_red_ci_class_ends_the_list_without_a_train_join() {
    let mut actions = Script::new(&[]);
    actions.green = false;
    let mut runner = Runner::new(inputs(), Limits::default());
    let outcome = runner.run(&mut actions, &mut Rows::default(), &mut tick());
    assert_eq!(
        outcome,
        Outcome::CiRed {
            head: HEAD.into(),
            pr: 7
        }
    );
    assert!(!actions.calls.contains(&"train_join"));
}

#[test]
fn fix_rounds_stop_at_the_limit_before_the_next_fix_runs() {
    let mut actions = Script::new(&[Verdict::RequestChanges; 5]);
    let limits = Limits {
        attempts: 2,
        fix_rounds: 2,
    };
    let mut runner = Runner::new(inputs(), limits);
    let outcome = runner.run(&mut actions, &mut Rows::default(), &mut tick());
    assert_eq!(outcome, Outcome::FixRoundsExhausted { rounds: 2 });
    assert_eq!(
        actions.calls.iter().filter(|call| **call == "fix").count(),
        2
    );
}

#[test]
fn output_the_contract_refuses_is_not_recorded() {
    let actions = Script::new(&[]);
    // An action that returns a blank stamp, which the contract refuses.
    struct Blank(Script);
    impl StepActions for Blank {
        fn claim(&mut self, i: &[String]) -> Result<Claim, String> {
            self.0.claim(i)
        }
        fn worktree(&mut self, c: &str, b: &str) -> Result<Worktree, String> {
            self.0.worktree(c, b)
        }
        fn write(&mut self, w: &str, b: &str) -> Result<Write, String> {
            self.0.write(w, b)
        }
        fn fast_checks(&mut self, _: &str) -> Result<FastChecks, String> {
            Ok(FastChecks { stamp: " ".into() })
        }
        fn review(&mut self, h: &str, s: &str) -> Result<Review, String> {
            self.0.review(h, s)
        }
        fn fix(&mut self, h: &str, s: &str, r: &str) -> Result<Fix, String> {
            self.0.fix(h, s, r)
        }
        fn push(&mut self, h: &str, s: &str, r: &str) -> Result<Push, String> {
            self.0.push(h, s, r)
        }
        fn ci_class(&mut self, h: &str, p: u64) -> Result<CiClass, String> {
            self.0.ci_class(h, p)
        }
        fn train_join(&mut self, h: &str, p: u64, r: u64, a: u32) -> Result<TrainJoin, String> {
            self.0.train_join(h, p, r, a)
        }
    }
    let mut runner = Runner::new(inputs(), Limits::default());
    let mut sink = Rows::default();
    let outcome = runner.run(&mut Blank(actions), &mut sink, &mut tick());
    assert!(matches!(
        outcome,
        Outcome::Invalid {
            step: "fast_checks",
            step_index: 3,
            ..
        }
    ));
    assert_eq!(runner.steps().rows.len(), 3);
    assert_eq!(sink.0, vec![1, 2, 3]);
}

#[test]
fn a_recorded_prefix_resumes_without_repeating_its_steps() {
    let mut first = Script::new(&[Verdict::RequestChanges, Verdict::Accept]);
    first.fail.push_back(("push", "network"));
    first.fail.push_back(("push", "network"));
    let mut runner = Runner::new(inputs(), Limits::default());
    let outcome = runner.run(&mut first, &mut Rows::default(), &mut tick());
    assert!(matches!(outcome, Outcome::Failed { step: "push", .. }));
    let saved = serde_json::to_value(runner.steps()).unwrap();
    let prefix: BatchSteps = serde_json::from_value(saved).unwrap();
    let before = prefix.rows.len();
    let mut resumed = Runner::resume(inputs(), Limits::default(), prefix).unwrap();
    let mut second = Script::new(&[]);
    let outcome = resumed.run(&mut second, &mut Rows::default(), &mut tick());
    assert!(matches!(outcome, Outcome::Joined { .. }));
    assert_eq!(second.calls, ["push", "ci_class", "train_join"]);
    assert_eq!(
        resumed.steps().rows[before].stage.attempt,
        3,
        "the two failed pushes of the prefix count"
    );
    resumed.steps().validate().unwrap();
}

#[test]
fn resume_refuses_the_steps_of_another_batch() {
    let steps = BatchSteps {
        version: 1,
        repo: "demo/repo".into(),
        batch_key: "other".into(),
        rows: vec![],
    };
    assert!(Runner::resume(inputs(), Limits::default(), steps).is_err());
}

#[test]
fn journal_rows_cover_the_keeper_stages_with_a_unique_attempt() {
    let mut actions = Script::new(&[Verdict::RequestChanges, Verdict::Accept]);
    let mut runner = Runner::new(inputs(), Limits::default());
    runner.run(&mut actions, &mut Rows::default(), &mut tick());
    let rows = &runner.steps().rows;
    let journal: Vec<_> = rows
        .iter()
        .enumerate()
        .filter_map(|(at, row)| journal_row("demo/repo", "demo:ITEM", BASE, at + 1, row))
        .collect();
    let stages: Vec<_> = journal
        .iter()
        .map(|row| row["stage"].as_str().unwrap())
        .collect();
    assert_eq!(
        stages,
        [
            "writing",
            "fast_checks",
            "review",
            "writing",
            "fast_checks",
            "review",
            "push",
            "ci"
        ]
    );
    let mut keys: Vec<_> = journal
        .iter()
        .map(|row| {
            (
                row["stage"].as_str().unwrap(),
                row["attempt"].as_u64().unwrap(),
            )
        })
        .collect();
    keys.sort();
    keys.dedup();
    assert_eq!(
        keys.len(),
        journal.len(),
        "stage and attempt are the keeper key"
    );
    assert_eq!(journal[3]["head"], FIXED);
}

#[test]
fn the_step_log_holds_the_whole_list_after_every_attempt() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("steps.json");
    let mut log = StepLog {
        path: path.clone(),
        journal_repo: None,
        item_keys: vec!["demo:ITEM".into()],
        base: BASE.into(),
    };
    let mut runner = Runner::new(inputs(), Limits::default());
    runner.run(&mut Script::new(&[]), &mut log, &mut tick());
    let saved: BatchSteps = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved.rows.len(), runner.steps().rows.len());
    saved.validate().unwrap();
    assert!(!path.with_extension("tmp").exists());
}

#[derive(Default)]
struct Fake {
    log: Vec<String>,
    head: String,
    fail_on: Option<&'static str>,
    pr_exists: bool,
    dirty: bool,
    branch_at: String,
}

impl CommandRunner for Fake {
    fn run(&mut self, _: &Path, argv: &[String]) -> Result<String, String> {
        self.log.push(argv.join(" "));
        if self
            .fail_on
            .is_some_and(|word| argv.iter().any(|arg| arg == word))
        {
            return Err("boom".into());
        }
        match argv[..argv.len().min(3)].join(" ").as_str() {
            "git rev-parse HEAD" => Ok(format!("{}\n", self.head)),
            "git rev-parse refs/heads/lane/x" => Ok(format!("{}\n", self.branch_at)),
            "git status --porcelain" if self.dirty => Ok(" M src/lib.rs\n".into()),
            "gh pr view" if !self.pr_exists => {
                self.pr_exists = true;
                Err("no pull request".into())
            }
            "gh pr view" => Ok("42\n".into()),
            _ => Ok(String::new()),
        }
    }
}

/// Shell steps over a real git directory, so the stamp path is the one the review gate reads.
fn shell(dir: &Path, fake: Fake) -> ShellSteps<Fake> {
    let worktree_dir = dir.join("wt");
    std::fs::create_dir_all(&worktree_dir).unwrap();
    let init = std::process::Command::new("git")
        .args(["init", "-q"])
        .current_dir(&worktree_dir)
        .status()
        .unwrap();
    assert!(init.success());
    let config = ShellConfig {
        repo_dir: dir.join("repo"),
        worktree_dir,
        branch: "lane/x".into(),
        checks: vec![vec!["check-a".into()], vec!["check-b".into()]],
        pr_title: "title".into(),
        pr_body: "body".into(),
    };
    ShellSteps {
        config,
        runner: fake,
    }
}

fn stamp_of(steps: &ShellSteps<Fake>, head: &str) -> std::path::PathBuf {
    pg_worker_core::worktree::fast_check_stamp(&steps.config.worktree_dir, head).unwrap()
}

#[test]
fn shell_steps_create_the_worktree_check_the_exact_head_and_push_a_draft_pr() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = shell(
        dir.path(),
        Fake {
            head: HEAD.into(),
            ..Fake::default()
        },
    );
    let created = steps.worktree(BASE).unwrap();
    assert_eq!(created.worktree, dir.path().join("wt").to_string_lossy());
    assert!(steps
        .runner
        .log
        .iter()
        .any(|line| line.starts_with("git worktree add -b lane/x ")));
    let checked = steps.fast_checks(HEAD).unwrap();
    assert_eq!(
        Path::new(&checked.stamp),
        stamp_of(&steps, HEAD),
        "the stamp is the review gate's receipt"
    );
    assert!(Path::new(&checked.stamp).is_file());
    let pushed = steps.push(HEAD, &checked.stamp).unwrap();
    assert_eq!(pushed.pr, 42);
    let log = steps.runner.log.join("\n");
    assert!(log.contains(&format!(
        "git push --atomic origin {HEAD}:refs/heads/lane/x +refs/plangraph/receipts/{HEAD}:refs/notes/plangraph-fast-checks-lane%2fx"
    )));
    let receipt = std::fs::read_to_string(&checked.stamp).unwrap();
    assert!(
        receipt.starts_with(&format!("head {HEAD}\nresult green\n"))
            && receipt.contains("check 1-check-a rc=0 wall=")
            && receipt.contains("check 2-check-b rc=0 wall="),
        "{receipt}"
    );
    assert!(!log.contains("--force"));
    assert!(
        log.contains("gh pr create --draft --head lane/x"),
        "view failed first, so the PR is created"
    );
}

#[test]
fn a_stale_head_a_dirty_tree_or_a_failing_check_leaves_no_stamp_and_blocks_the_push() {
    let dir = tempfile::tempdir().unwrap();
    let fake = |head: &str, dirty, fail_on| Fake {
        head: head.into(),
        dirty,
        fail_on,
        ..Fake::default()
    };
    let mut stale = shell(dir.path(), fake(FIXED, false, None));
    assert!(stale.fast_checks(HEAD).unwrap_err().contains("not"));
    let mut dirty = shell(dir.path(), fake(HEAD, true, None));
    assert!(dirty.fast_checks(HEAD).unwrap_err().contains("uncommitted"));
    assert!(
        dirty
            .runner
            .log
            .iter()
            .all(|line| !line.starts_with("check-")),
        "no check runs on a dirty tree"
    );
    let mut red = shell(dir.path(), fake(HEAD, false, Some("check-b")));
    assert!(red.fast_checks(HEAD).is_err());
    let stamp = stamp_of(&red, HEAD);
    assert!(!stamp.exists());
    let err = red.push(HEAD, &stamp.to_string_lossy()).unwrap_err();
    assert!(err.contains("no fast-checks stamp"));
    assert!(red.runner.log.iter().all(|line| !line.contains("push")));
}

#[test]
fn a_failed_worktree_add_is_cleaned_so_the_retry_starts_clean() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = shell(
        dir.path(),
        Fake {
            fail_on: Some("add"),
            branch_at: BASE.into(),
            ..Fake::default()
        },
    );
    assert_eq!(steps.worktree(BASE).unwrap_err(), "boom");
    let log = steps.runner.log.join("\n");
    assert!(log.contains("git worktree remove --force"));
    assert!(
        log.contains("git branch -D lane/x"),
        "a branch still at the base is dropped"
    );
    // A branch that moved on holds work and is kept.
    let mut moved = shell(
        dir.path(),
        Fake {
            fail_on: Some("add"),
            branch_at: HEAD.into(),
            ..Fake::default()
        },
    );
    moved.worktree(BASE).unwrap_err();
    assert!(!moved.runner.log.join("\n").contains("branch -D"));
}

struct Unsaved;

impl StageSink for Unsaved {
    fn record(&mut self, _: &BatchSteps) -> Result<(), String> {
        Err("disk full".into())
    }
}

#[test]
fn a_list_that_cannot_be_saved_stops_the_run() {
    let mut actions = Script::new(&[]);
    let mut runner = Runner::new(inputs(), Limits::default());
    let outcome = runner.run(&mut actions, &mut Unsaved, &mut tick());
    assert_eq!(
        outcome,
        Outcome::NotSaved {
            step_index: 0,
            reason: "disk full".into()
        }
    );
    assert_eq!(actions.calls, ["claim"], "no step runs after a save failed");
}

#[test]
fn journal_rows_are_written_per_item_key() {
    let mut runner = Runner::new(inputs(), Limits::default());
    runner.run(&mut Script::new(&[]), &mut Rows::default(), &mut tick());
    let row = &runner.steps().rows[2];
    let journal = journal_row("demo/repo", "demo:ITEM", BASE, 3, row).unwrap();
    assert_eq!(journal["batch_key"], "demo:ITEM");
}
