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
        "--train-cmd",
        "train-desk join",
        "--ci-workflow",
        "CI",
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
    assert!(args.journal);
    assert_eq!(args.ci_workflows, ["CI"]);
}

#[test]
fn a_missing_or_repeated_flag_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = call(dir.path());
    args.extend(strings(&["--repo", "other/repo"]));
    assert_eq!(parse(&args).unwrap_err(), "--repo given twice");
    let args = call(dir.path());
    let without: Vec<String> = args[..args.iter().position(|a| a == "--brief").unwrap()].to_vec();
    assert!(parse(&without).unwrap_err().contains("--brief is required"));
    assert!(parse(&strings(&["run-abc", "--nope", "x"])).is_err());
}

#[test]
fn commands_split_into_argv() {
    let dir = tempfile::tempdir().unwrap();
    let mut args = call(dir.path());
    args.extend(strings(&["--keeper-cmd", "pg --json", "--no-journal"]));
    let args = parse(&args).unwrap();
    assert_eq!(args.train_cmd, ["train-desk", "join"]);
    assert_eq!(args.keeper_cmd.unwrap(), ["pg", "--json"]);
    assert!(!args.journal);
}

#[test]
fn a_call_without_a_train_command_or_ci_workflow_is_refused_before_anything_runs() {
    let dir = tempfile::tempdir().unwrap();
    let full = call(dir.path());
    let cut = |flag: &str| -> Vec<String> {
        let at = full.iter().position(|arg| arg == flag).unwrap();
        full[..at].to_vec()
    };
    assert!(parse(&cut("--train-cmd"))
        .unwrap_err()
        .contains("--train-cmd is required"));
    assert!(parse(&cut("--ci-workflow"))
        .unwrap_err()
        .contains("--ci-workflow is required"));
}

#[test]
fn the_round_cap_allows_eight_reviews_and_so_seven_fixes() {
    assert_eq!(
        limits().fix_rounds as u64 + 1,
        crate::round_progress::MAX_ROUNDS
    );
}

#[test]
fn a_missing_steps_file_starts_the_list() {
    let dir = tempfile::tempdir().unwrap();
    let runner = load(&dir.path().join("steps.json"), inputs(dir.path()), None).unwrap();
    assert!(runner.steps().rows.is_empty());
}

#[test]
fn a_steps_file_that_is_not_a_list_is_refused_with_its_path() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("steps.json"), "{}").unwrap();
    let error = load(&dir.path().join("steps.json"), inputs(dir.path()), None)
        .err()
        .unwrap();
    assert!(error.contains("steps.json"), "{error}");
}

/// Actions that record the steps they were asked to run.
#[derive(Default)]
struct Calls(Vec<&'static str>, Option<usize>);

impl Calls {
    /// Record a call; the call numbered `die_on` kills the process before it returns.
    fn enter(&mut self, step: &'static str) {
        self.0.push(step);
        if self.1 == Some(self.0.len() - 1) {
            panic!("killed during {step}");
        }
    }
}

impl StepActions for Calls {
    fn claim(&mut self, _: &[String]) -> Result<Claim, String> {
        self.enter("claim");
        Ok(Claim { claim: "c".into() })
    }
    fn worktree(&mut self, _: &str, _: &str) -> Result<Worktree, String> {
        self.enter("worktree");
        Ok(Worktree {
            worktree: "w".into(),
        })
    }
    fn write(&mut self, _: &str, _: &str) -> Result<Write, String> {
        self.enter("write");
        Ok(Write {
            head: HEAD.into(),
            session: "s".into(),
        })
    }
    fn fast_checks(&mut self, _: &str) -> Result<FastChecks, String> {
        self.enter("fast_checks");
        Ok(FastChecks { stamp: "st".into() })
    }
    fn review(&mut self, _: &str, _: &str) -> Result<Review, String> {
        self.enter("review");
        Ok(Review {
            verdict: Verdict::Accept,
            review: "run-1".into(),
        })
    }
    fn fix(&mut self, _: &str, _: &str, _: &str) -> Result<Fix, String> {
        unreachable!()
    }
    fn push(&mut self, _: &str, _: &str, _: &str) -> Result<Push, String> {
        self.enter("push");
        Ok(Push { pr: 5 })
    }
    fn ci_class(&mut self, _: &str, _: u64) -> Result<CiClass, String> {
        self.enter("ci_class");
        Ok(CiClass {
            classification: Classification::Green,
            run: 2,
            attempt: 1,
        })
    }
    fn train_join(&mut self, _: &str, _: u64, _: u64, _: u32) -> Result<TrainJoin, String> {
        self.enter("train_join");
        Ok(TrainJoin {
            train: "t".into(),
            manifest: "m".into(),
        })
    }
}

/// Start the subcommand's `drive` on the call; `die_on` kills it during that action call.
fn start(dir: &Path, die_on: Option<usize>) -> (Calls, Option<i32>) {
    let args = Args {
        journal: false,
        ..parse(&call(dir)).unwrap()
    };
    let mut calls = Calls(Vec::new(), die_on);
    let code = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        drive(&args, None, &mut calls, &mut Vec::new()).unwrap()
    }));
    (calls, code.ok())
}

fn rows_saved(dir: &Path) -> usize {
    let saved: Value =
        serde_json::from_slice(&std::fs::read(dir.join("steps.json")).unwrap()).unwrap();
    saved["rows"].as_array().unwrap().len()
}

#[test]
fn a_killed_run_resumes_from_the_steps_file() {
    let dir = tempfile::tempdir().unwrap();
    // Killed during the review (the fifth action call): four rows are on disk.
    let (killed, code) = start(dir.path(), Some(4));
    assert_eq!(code, None, "the run did not finish");
    assert_eq!(
        killed.0,
        ["claim", "worktree", "write", "fast_checks", "review"]
    );
    assert_eq!(rows_saved(dir.path()), 4);

    // The same call again resumes at the review: nothing before it runs twice.
    let (second, code) = start(dir.path(), None);
    assert_eq!(code, Some(0));
    assert_eq!(second.0, ["review", "push", "ci_class", "train_join"]);
    assert_eq!(rows_saved(dir.path()), 8);

    // A third start has nothing left to run and reports the same join.
    let (third, code) = start(dir.path(), None);
    assert_eq!(code, Some(0));
    assert!(third.0.is_empty());
}

#[test]
fn a_steps_file_of_another_batch_is_not_resumed() {
    let dir = tempfile::tempdir().unwrap();
    start(dir.path(), None);
    let mut other = inputs(dir.path());
    other.batch_key = "batch-2".into();
    let error = load(&dir.path().join("steps.json"), other, None)
        .err()
        .unwrap();
    assert!(error.contains("another repo or batch"), "{error}");
}

#[test]
fn the_outcome_sets_the_exit_code_and_the_report() {
    let dir = tempfile::tempdir().unwrap();
    let runner = load(&dir.path().join("none.json"), inputs(dir.path()), None).unwrap();
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

/// A list saved by a finished run, to be resumed with changed inputs.
fn saved_list(dir: &Path) {
    start(dir, None);
}

#[test]
fn saved_steps_for_other_items_base_or_brief_are_refused_before_a_step_runs() {
    let dir = tempfile::tempdir().unwrap();
    saved_list(dir.path());
    let path = dir.path().join("steps.json");
    type Edit = fn(&mut Inputs);
    let change: [(&str, Edit); 3] = [
        ("items", |i| i.items = vec!["demo:Z".into()]),
        ("base", |i| i.base = HEAD.into()),
        ("brief", |i| i.brief = "/elsewhere/brief.md".into()),
    ];
    for (what, edit) in change {
        let mut other = inputs(dir.path());
        edit(&mut other);
        let error = load(&path, other, None).err().unwrap();
        assert!(error.contains(&format!("other {what}")), "{what}: {error}");
    }
}

#[test]
fn saved_steps_of_another_native_run_are_refused() {
    let dir = tempfile::tempdir().unwrap();
    saved_list(dir.path());
    let path = dir.path().join("steps.json");
    // The recorded worktree is `w`; the run this call resolved has its own checkout.
    let error = load(&path, inputs(dir.path()), Some(Path::new("/runs/other/wt")))
        .err()
        .unwrap();
    assert!(error.contains("native run"), "{error}");
    assert!(load(&path, inputs(dir.path()), Some(Path::new("w"))).is_ok());
}

#[path = "step_run_bind_tests.rs"]
mod bind;
