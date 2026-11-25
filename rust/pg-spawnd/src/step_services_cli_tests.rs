use crate::step_runner_shell::CommandRunner;
use crate::step_services::*;
use crate::step_services_cli::*;
use std::{collections::VecDeque, path::Path};

const HEAD: &str = "1111111111111111111111111111111111111111";

fn run(id: u64, status: &str, conclusion: Option<&str>) -> CiRun {
    CiRun {
        name: "CI".into(),
        event: "pull_request".into(),
        id,
        attempt: 1,
        status: status.into(),
        conclusion: conclusion.map(str::to_owned),
    }
}

/// Runner that answers by the first words of the command and keeps every command.
struct Script {
    answers: VecDeque<Result<String, String>>,
    seen: Vec<Vec<String>>,
}

impl Script {
    fn new(answers: Vec<Result<&str, &str>>) -> Self {
        Self {
            answers: answers
                .into_iter()
                .map(|answer| answer.map(str::to_owned).map_err(str::to_owned))
                .collect(),
            seen: Vec::new(),
        }
    }
}

impl CommandRunner for &mut Script {
    fn run(&mut self, _: &Path, argv: &[String]) -> Result<String, String> {
        self.seen.push(argv.to_vec());
        self.answers.pop_front().unwrap_or(Err("no answer".into()))
    }
}

/// A key file of its own: a fresh path in a directory that lives as long as the test process.
fn key_file() -> std::path::PathBuf {
    static DIR: std::sync::OnceLock<tempfile::TempDir> = std::sync::OnceLock::new();
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let dir = DIR.get_or_init(|| tempfile::tempdir().unwrap());
    dir.path().join(format!(
        "claim-key-{}",
        N.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
    ))
}

fn keys() -> Vec<String> {
    vec!["demo:A".into(), "demo:B".into()]
}

#[test]
fn keeper_cli_reads_the_epochs_of_a_claim_batch() {
    let mut script = Script::new(vec![Ok(
        r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","epoch":9}]}"#,
    )]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into(), "--json".into()],
        key_file: key_file(),
    };
    let held = keeper.claim_batch(&keys()).unwrap();
    assert_eq!(held, vec![("demo:A".into(), 3), ("demo:B".into(), 9)]);
    assert_eq!(script.seen[0][0], "env");
    assert!(script.seen[0][1].starts_with("PG_IDEMPOTENCY_KEY="));
    assert_eq!(
        script.seen[0][2..],
        ["pg", "--json", "claim-batch", "demo:A", "demo:B"]
    );
}

const GRANTED: &str = r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","epoch":9}]}"#;

#[test]
fn a_claim_killed_after_the_keeper_granted_it_retries_with_the_same_key() {
    let key_file = key_file();
    let mut first = Script::new(vec![Ok(GRANTED)]);
    KeeperCli {
        runner: &mut first,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    }
    .claim_batch(&keys())
    .unwrap();
    // The process died before its row was saved. The next one is a new KeeperCli on the same
    // file: it must ask the keeper with the key of the first call, not a fresh one.
    let mut second = Script::new(vec![Ok(GRANTED)]);
    let held = KeeperCli {
        runner: &mut second,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file,
    }
    .claim_batch(&keys())
    .unwrap();
    assert_eq!(held.len(), 2);
    assert_eq!(first.seen[0][1], second.seen[0][1], "one key for one claim");
}

#[test]
fn a_refused_claim_settles_its_key_so_the_next_attempt_asks_anew() {
    let key_file = key_file();
    let refused = r#"{"results":[{"key":"demo:A","error":"claimed"}]}"#;
    let mut first = Script::new(vec![Ok(refused)]);
    let mut keeper = KeeperCli {
        runner: &mut first,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    };
    assert!(keeper.claim_batch(&keys()).is_err());
    assert!(!key_file.exists(), "a replay of the refusal would be stale");
}

#[test]
fn keeper_cli_releases_only_the_epochs_this_attempt_got() {
    // demo:A was granted at epoch 3, demo:B is held by someone else: only A goes back.
    let mut script = Script::new(vec![
        Ok(
            r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed","msg":"held"}]}"#,
        ),
        Ok("{}"),
    ]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file(),
    };
    let error = keeper.claim_batch(&keys()).unwrap_err();
    assert!(error.contains("demo:B"), "{error}");
    assert_eq!(
        script.seen.len(),
        2,
        "no release for the key another batch holds"
    );
    assert_eq!(
        script.seen[1],
        ["env", "PG_EPOCH=3", "pg", "release", "demo:A"]
    );
}

#[test]
fn keeper_cli_reports_a_release_that_failed() {
    let mut script = Script::new(vec![
        Ok(r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed"}]}"#),
        Err("pg exit 2: refused"),
    ]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file(),
    };
    let error = keeper.claim_batch(&keys()).unwrap_err();
    assert!(error.contains("release failed: demo:A@3"), "{error}");
}

#[test]
fn keeper_cli_says_when_the_outcome_of_the_claim_is_unknown() {
    let mut script = Script::new(vec![Err("pg: unreachable")]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file(),
    };
    let error = keeper.claim_batch(&keys()).unwrap_err();
    assert!(error.contains("outcome unknown"), "{error}");
}

#[test]
fn keeper_cli_refuses_a_row_without_an_epoch() {
    let mut script = Script::new(vec![Ok(r#"{"results":[{"key":"demo:A","error":"held"}]}"#)]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file(),
    };
    assert!(keeper.claim_batch(&keys()).unwrap_err().contains("refused"));
}

#[test]
fn gh_ci_reads_only_the_runs_of_this_pull_request() {
    let mut script = Script::new(vec![Ok(r#"{"workflow_runs":[
            {"id":5,"name":"CI","event":"pull_request","run_attempt":2,"status":"completed","conclusion":"success","pull_requests":[{"number":12}]},
            {"id":6,"name":"CI","event":"pull_request","run_attempt":1,"status":"queued","conclusion":null,"pull_requests":[{"number":12}]},
            {"id":7,"name":"CI","event":"pull_request","run_attempt":1,"status":"completed","conclusion":"failure","pull_requests":[{"number":99}]},
            {"id":8,"name":"CI","event":"pull_request","run_attempt":1,"status":"completed","conclusion":"success","pull_requests":[]}]}"#)]);
    let mut ci = GhCi {
        runner: &mut script,
        dir: ".".into(),
        repo: "demo/repo".into(),
    };
    let runs = ci.runs(HEAD, 12).unwrap();
    assert_eq!(
        runs.len(),
        2,
        "another pull request's run and an unbound run are left out"
    );
    assert_eq!(runs[0], run(5, "completed", Some("success")).tap_attempt(2));
    assert_eq!(runs[1].conclusion, None);
    assert_eq!(
        script.seen[0],
        [
            "gh",
            "api",
            &format!("repos/demo/repo/actions/runs?head_sha={HEAD}&per_page=100")
        ]
    );
}

trait TapAttempt {
    fn tap_attempt(self, attempt: u32) -> Self;
}

impl TapAttempt for CiRun {
    fn tap_attempt(mut self, attempt: u32) -> Self {
        self.attempt = attempt;
        self
    }
}

#[test]
fn train_command_passes_the_ci_run_it_consumes() {
    let mut script = Script::new(vec![Ok(r#"{"train":"t-1","manifest":"m-1"}"#)]);
    let mut desk = TrainCommand {
        runner: &mut script,
        dir: ".".into(),
        argv: vec!["train-desk".into(), "join".into()],
    };
    let joined = desk.join(HEAD, 12, 6, 2).unwrap();
    assert_eq!(
        (joined.train.as_str(), joined.manifest.as_str()),
        ("t-1", "m-1")
    );
    assert_eq!(
        script.seen[0],
        [
            "train-desk",
            "join",
            "--head",
            HEAD,
            "--pr",
            "12",
            "--run",
            "6",
            "--attempt",
            "2"
        ]
    );
}

#[test]
fn train_command_without_a_command_fails_with_a_reason() {
    let mut script = Script::new(vec![]);
    let mut desk = TrainCommand {
        runner: &mut script,
        dir: ".".into(),
        argv: vec![],
    };
    assert!(desk
        .join(HEAD, 1, 1, 1)
        .unwrap_err()
        .contains("no train join command"));
}

#[test]
fn gh_ci_reads_whether_the_pull_request_is_a_draft_lane_part_at_this_head() {
    let at = |draft: bool, branch: &str, sha: &str| {
        format!(r#"{{"draft":{draft},"head":{{"ref":"{branch}","sha":"{sha}"}}}}"#)
    };
    let newer = "2".repeat(40);
    let cases = [
        (at(true, "lane/batch-1", HEAD), true),
        (at(false, "lane/batch-1", HEAD), false),
        (at(true, "batch/x", HEAD), false),
        // The pull request moved on: its draft state is not evidence for a run of the old head.
        (at(true, "lane/batch-1", &newer), false),
    ];
    for (pull, expected) in cases {
        let mut script = Script::new(vec![Ok(&pull)]);
        let mut ci = GhCi {
            runner: &mut script,
            dir: ".".into(),
            repo: "demo/repo".into(),
        };
        assert_eq!(ci.draft_lane_part(12, HEAD).unwrap(), expected, "{pull}");
        assert_eq!(script.seen[0], ["gh", "api", "repos/demo/repo/pulls/12"]);
    }
}

const MIXED: &str =
    r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed"}]}"#;

fn keeper<'a>(script: &'a mut Script, key_file: &std::path::Path) -> KeeperCli<&'a mut Script> {
    KeeperCli {
        runner: script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.to_owned(),
    }
}

#[test]
fn a_release_whose_answer_was_lost_after_the_keeper_committed_it_is_confirmed() {
    let key_file = key_file();
    // The release is refused on replay and the item is open again: the grant is gone.
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg exit 2: demo:A is open, not claimed"),
        Ok(r#"{"key":"demo:A","state":"open","epoch":3}"#),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(!error.contains("release failed"), "{error}");
    assert_eq!(script.seen[2], ["pg", "show", "demo:A"]);
    assert!(
        !key_file.exists(),
        "the cleanup is confirmed, so the key goes"
    );
}

#[test]
fn a_grant_that_the_keeper_still_holds_keeps_the_key() {
    let key_file = key_file();
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg: unreachable"),
        Ok(r#"{"key":"demo:A","state":"claimed","epoch":3}"#),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(error.contains("release failed: demo:A@3"), "{error}");
    assert!(key_file.exists());
}

#[test]
fn a_release_is_confirmed_when_another_claim_holds_the_item_at_a_later_epoch() {
    let key_file = key_file();
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg exit 2: epoch mismatch"),
        Ok(r#"{"key":"demo:A","state":"claimed","epoch":4}"#),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(!error.contains("release failed"), "{error}");
}

#[test]
fn a_kill_during_the_cleanup_of_a_partial_grant_keeps_the_key() {
    let key_file = key_file();
    let mixed = r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed"}]}"#;
    // The release of demo:A does not finish (the process is killed, or the release fails).
    let mut first = Script::new(vec![Ok(mixed), Err("killed")]);
    let mut keeper = KeeperCli {
        runner: &mut first,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    };
    assert!(keeper.claim_batch(&keys()).is_err());
    assert!(
        key_file.exists(),
        "the grant of demo:A is not yet given back"
    );
    // The next process asks with the same key, so the keeper replays the grant it holds.
    let mut second = Script::new(vec![Ok(mixed), Ok("{}")]);
    let mut again = KeeperCli {
        runner: &mut second,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    };
    assert!(again.claim_batch(&keys()).is_err());
    assert_eq!(first.seen[0][1], second.seen[0][1]);
    assert!(!key_file.exists(), "dropped once the cleanup is confirmed");
}
