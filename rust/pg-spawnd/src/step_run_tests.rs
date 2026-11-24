use crate::step_run::*;
use pg_spawnd::step_runner::*;
use pg_worker_core::batch_steps::*;
use serde_json::Value;
use std::path::Path;

const BASE: &str = "0000000000000000000000000000000000000000";
const HEAD: &str = "1111111111111111111111111111111111111111";

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

fn call(dir: &Path) -> Vec<String> {
    let brief = dir.join("brief.md");
    std::fs::write(&brief, "brief").unwrap();
    strings(&[
        "run-abc",
        "--steps",
        dir.join("steps.json").to_str().unwrap(),
        "--repo",
        "demo/repo",
        "--batch-key",
        "batch-1",
        "--items",
        "demo:A,demo:B",
        "--base",
        BASE,
        "--brief",
        brief.to_str().unwrap(),
    ])
}

fn inputs(dir: &Path) -> Inputs {
    parse(&call(dir)).unwrap().inputs
}

#[test]
fn the_call_names_repo_batch_items_base_brief_and_steps_file() {
    let dir = tempfile::tempdir().unwrap();
    let args = parse(&call(dir.path())).unwrap();
    assert_eq!(args.run_id, "run-abc");
    assert_eq!(args.inputs.items, ["demo:A", "demo:B"]);
    assert_eq!(args.inputs.base, BASE);
    assert!(args.inputs.brief.ends_with("brief.md"));
    assert_eq!(args.steps, dir.path().join("steps.json"));
    assert!(args.journal && args.checks.is_empty());
}

#[test]
fn a_missing_or_repeated_flag_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = call(dir.path());
    args.extend(strings(&["--repo", "other/repo"]));
    assert_eq!(parse(&args).unwrap_err(), "--repo given twice");
    let args = call(dir.path());
    let without: Vec<String> = args[..args.iter().position(|a| a == "--base").unwrap()].to_vec();
    assert!(parse(&without).unwrap_err().contains("--brief is required"));
    assert!(parse(&strings(&["run-abc", "--nope", "x"])).is_err());
}

#[test]
fn checks_and_commands_split_into_argv() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = call(dir.path());
    args.extend(strings(&[
        "--check",
        "cargo fmt --check",
        "--check",
        "make lint",
        "--train-cmd",
        "train-desk join",
        "--no-journal",
    ]));
    let args = parse(&args).unwrap();
    assert_eq!(
        args.checks,
        [
            strings(&["cargo", "fmt", "--check"]),
            strings(&["make", "lint"])
        ]
    );
    assert_eq!(args.train_cmd, ["train-desk", "join"]);
    assert!(!args.journal);
}

#[test]
fn the_fix_round_limit_is_the_round_cap() {
    assert_eq!(
        limits().fix_rounds as u64,
        crate::round_progress::MAX_ROUNDS
    );
}

#[test]
fn a_missing_steps_file_starts_the_list() {
    let dir = tempfile::tempdir().unwrap();
    let runner = load(&dir.path().join("steps.json"), inputs(dir.path())).unwrap();
    assert!(runner.steps().rows.is_empty());
}

#[test]
fn a_steps_file_that_is_not_a_list_is_refused_with_its_path() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("steps.json"), "{}").unwrap();
    let error = load(&dir.path().join("steps.json"), inputs(dir.path()))
        .err()
        .unwrap();
    assert!(error.contains("steps.json"), "{error}");
}

/// Actions that record the steps they were asked to run.
#[derive(Default)]
struct Calls(Vec<&'static str>);

impl StepActions for Calls {
    fn claim(&mut self, _: &[String]) -> Result<Claim, String> {
        self.0.push("claim");
        Ok(Claim { claim: "c".into() })
    }
    fn worktree(&mut self, _: &str, _: &str) -> Result<Worktree, String> {
        self.0.push("worktree");
        Ok(Worktree {
            worktree: "w".into(),
        })
    }
    fn write(&mut self, _: &str, _: &str) -> Result<Write, String> {
        self.0.push("write");
        Ok(Write {
            head: HEAD.into(),
            session: "s".into(),
        })
    }
    fn fast_checks(&mut self, _: &str) -> Result<FastChecks, String> {
        self.0.push("fast_checks");
        Ok(FastChecks { stamp: "st".into() })
    }
    fn review(&mut self, _: &str, _: &str) -> Result<Review, String> {
        self.0.push("review");
        Ok(Review {
            verdict: Verdict::Accept,
            review: "run-1".into(),
        })
    }
    fn fix(&mut self, _: &str, _: &str, _: &str) -> Result<Fix, String> {
        unreachable!()
    }
    fn push(&mut self, _: &str, _: &str, _: &str) -> Result<Push, String> {
        self.0.push("push");
        Ok(Push { pr: 5 })
    }
    fn ci_class(&mut self, _: &str, _: u64) -> Result<CiClass, String> {
        self.0.push("ci_class");
        Ok(CiClass {
            classification: Classification::Green,
            run: 2,
            attempt: 1,
        })
    }
    fn train_join(&mut self, _: &str, _: u64, _: u64, _: u32) -> Result<TrainJoin, String> {
        self.0.push("train_join");
        Ok(TrainJoin {
            train: "t".into(),
            manifest: "m".into(),
        })
    }
}

fn run_to_end(dir: &Path) -> (Calls, Outcome, usize) {
    let args = parse(&call(dir)).unwrap();
    let mut runner = load(&args.steps, args.inputs.clone()).unwrap();
    let mut sink = log_for(&Args {
        journal: false,
        ..args
    });
    let mut calls = Calls::default();
    let mut now = 0;
    let outcome = runner.run(&mut calls, &mut sink, &mut || {
        now += 1;
        now
    });
    (calls, outcome, runner.steps().rows.len())
}

#[test]
fn a_killed_run_resumes_from_the_steps_file() {
    let dir = tempfile::tempdir().unwrap();
    let (first, outcome, rows) = run_to_end(dir.path());
    assert!(matches!(outcome, Outcome::Joined { .. }));
    assert_eq!(rows, 8);
    assert_eq!(first.0[0], "claim");
    // The process died after the fast checks of the written head: keep the first four rows.
    let path = dir.path().join("steps.json");
    let mut saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    saved["rows"].as_array_mut().unwrap().truncate(4);
    std::fs::write(&path, saved.to_string()).unwrap();

    let (second, outcome, rows) = run_to_end(dir.path());
    assert_eq!(
        second.0,
        ["review", "push", "ci_class", "train_join"],
        "nothing before the kill reruns"
    );
    assert!(matches!(outcome, Outcome::Joined { .. }));
    assert_eq!(rows, 8);
    // The file holds the whole list again, so a third start has nothing left to run.
    let (third, outcome, _) = run_to_end(dir.path());
    assert!(third.0.is_empty());
    assert!(matches!(outcome, Outcome::Joined { .. }));
}

#[test]
fn a_steps_file_of_another_batch_is_not_resumed() {
    let dir = tempfile::tempdir().unwrap();
    run_to_end(dir.path());
    let mut other = inputs(dir.path());
    other.batch_key = "batch-2".into();
    let error = load(&dir.path().join("steps.json"), other).err().unwrap();
    assert!(error.contains("another repo or batch"), "{error}");
}

#[test]
fn the_outcome_sets_the_exit_code_and_the_report() {
    let dir = tempfile::tempdir().unwrap();
    let runner = load(&dir.path().join("none.json"), inputs(dir.path())).unwrap();
    let joined = Outcome::Joined {
        train: "t".into(),
        manifest: "m".into(),
    };
    assert_eq!(report(&joined, &runner).0, 0);
    assert_eq!(report(&joined, &runner).1["train"], "t");
    let red = Outcome::CiRed {
        head: HEAD.into(),
        pr: 5,
    };
    assert_eq!(report(&red, &runner).0, 3);
    let failed = Outcome::Failed {
        step: "push",
        step_index: 5,
        reason: "refused".into(),
    };
    let (code, line) = report(&failed, &runner);
    assert_eq!(
        (code, line["step"].as_str(), line["reason"].as_str()),
        (1, Some("push"), Some("refused"))
    );
}

#[test]
fn the_subcommand_refuses_a_run_that_has_no_record() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = call(dir.path());
    args.extend(strings(&["--state", dir.path().to_str().unwrap()]));
    let error = execute(args, &mut Vec::new()).unwrap_err();
    assert!(
        error.contains("record") || error.contains("unknown run id"),
        "{error}"
    );
    assert!(
        !dir.path().join("steps.json").exists(),
        "nothing was started"
    );
}
