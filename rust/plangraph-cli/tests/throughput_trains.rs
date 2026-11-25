#![cfg(unix)]
//! Train acceptance joins, hourly bounds and database upgrades of `plangraph throughput`.

use rusqlite::Connection;
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

fn output(command: &mut Command) -> String {
    let result = command.output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    String::from_utf8(result.stdout).unwrap()
}

fn git_command(repo: &Path) -> Command {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    command
        .current_dir(repo)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .args(["-c", "commit.gpgsign=false"]);
    command
}

fn git(repo: &Path, args: &[&str]) -> String {
    output(git_command(repo).args(args))
}

fn dated(repo: &Path, date: &str) -> Command {
    let mut command = git_command(repo);
    command
        .env("GIT_AUTHOR_DATE", date)
        .env("GIT_COMMITTER_DATE", date);
    command
}

/// A repository whose main received a train of six parts of two commits each, a landing row for it
/// (the train took 60 minutes, 2 reruns, 1 left out, 3 fix tasks) and one accepted writer run per part head.
struct Train {
    _root: tempfile::TempDir,
    repo: PathBuf,
    db: PathBuf,
    bin: PathBuf,
    runs: PathBuf,
    snapshot: PathBuf,
    landed: String,
    heads: Vec<String>,
}

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    /// Main takes the train with one merge commit.
    PrMerge,
    /// Main is fast-forwarded to the tested head: the parts' merges are first-parent arrivals of their own.
    TestedFastForward,
}

fn train(mode: Mode) -> Train {
    train_with(mode, false, &[])
}

/// With `direct`, one more accepted head reached main on its own, after the base and before the train was built.
/// `skip` lists parts (0-based) whose writer run has no accepted review record.
fn train_with(mode: Mode, direct: bool, skip: &[usize]) -> Train {
    let root = tempfile::tempdir().unwrap();
    let repo = root.path().to_path_buf();
    let bin = repo.join("bin");
    fs::create_dir(&bin).unwrap();
    let git_path = std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|dir| dir.join("git"))
        .find(|path| path.is_file())
        .expect("git on PATH");
    std::os::unix::fs::symlink(git_path.canonicalize().unwrap(), bin.join("git")).unwrap();
    git(&repo, &["init", "-q", "-b", "main"]);
    let at = "2026-11-21T10:00:00Z";
    output(dated(&repo, at).args(["commit", "-q", "--allow-empty", "-m", "base"]));
    let base = git(&repo, &["rev-parse", "HEAD"]).trim().to_owned();
    let mut direct_head = None;
    if direct {
        git(&repo, &["checkout", "-q", "-b", "direct", "main"]);
        output(dated(&repo, at).args(["commit", "-q", "--allow-empty", "-m", "landed on its own"]));
        direct_head = Some(git(&repo, &["rev-parse", "HEAD"]).trim().to_owned());
        git(&repo, &["checkout", "-q", "main"]);
        output(dated(&repo, at).args([
            "merge",
            "-q",
            "--no-ff",
            "-m",
            "merge on its own",
            "direct",
        ]));
    }
    git(&repo, &["checkout", "-q", "-b", "train"]);
    let mut heads = Vec::new();
    for part in 1..=6 {
        git(
            &repo,
            &["checkout", "-q", "-b", &format!("part{part}"), "main"],
        );
        for step in 1..=2 {
            output(dated(&repo, at).args([
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                &format!("part {part} step {step}"),
            ]));
        }
        heads.push(git(&repo, &["rev-parse", "HEAD"]).trim().to_owned());
        git(&repo, &["checkout", "-q", "train"]);
        output(dated(&repo, at).args([
            "merge",
            "-q",
            "--no-ff",
            "-m",
            &format!("train takes {part}"),
            &format!("part{part}"),
        ]));
    }
    git(&repo, &["checkout", "-q", "main"]);
    match mode {
        Mode::PrMerge => {
            output(dated(&repo, "2026-11-21T12:00:00Z").args([
                "merge",
                "-q",
                "--no-ff",
                "-m",
                "land train",
                "train",
            ]));
        }
        Mode::TestedFastForward => {
            git(&repo, &["merge", "-q", "--ff-only", "train"]);
        }
    }
    let landed = git(&repo, &["rev-parse", "HEAD"]).trim().to_owned();
    let db = repo.join("landing.sqlite3");
    let connection = Connection::open(&db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE landing(repo TEXT,landed_sha TEXT,train TEXT,parts TEXT,
         build_started_at TEXT,landed_at TEXT,ci_reruns INTEGER,left_out INTEGER,fix_tasks INTEGER,
         mode TEXT,PRIMARY KEY(repo,landed_sha));",
        )
        .unwrap();
    // An earlier landing (the base) bounds the train from below.
    connection.execute(
        "INSERT INTO landing VALUES('fixture/repo',?1,'t0','[0]','2026-11-21T08:00:00Z','2026-11-21T09:30:00Z',0,0,0,'tested-ff')",
        [&base],
    ).unwrap();
    connection.execute(
        "INSERT INTO landing VALUES('fixture/repo',?1,'t','[1,2,3,4,5,6]','2026-11-21T11:00:00Z','2026-11-21T12:00:00Z',2,1,3,?2)",
        rusqlite::params![
            &landed,
            if mode == Mode::PrMerge { "train-pr-merge" } else { "tested-ff" }
        ],
    ).unwrap();
    let snapshot = repo.join("keeper.json");
    fs::write(&snapshot, json!({"items":[]}).to_string()).unwrap();
    let runs = repo.join("runs");
    fs::create_dir_all(&runs).unwrap();
    for (n, head) in heads.iter().chain(direct_head.iter()).enumerate() {
        if skip.contains(&n) {
            continue;
        }
        let dir = runs.join(format!("run-{n}"));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("record.json"), json!({
            "run_id":format!("run-{n}"),"run_kind":"native-codex","host":"fixture","status":"completed",
            "started":"2026-11-21T08:00:00Z","ended":"2026-11-21T09:00:00Z","final_head":head,
            "review":{"verdict":"ACCEPT","head":head,"accepted_at":"2026-11-21T09:00:00Z"}
        }).to_string()).unwrap();
        fs::write(
            dir.join("ledger.jsonl"),
            "{\"model\":\"fixture-model\",\"tokens\":100}\n",
        )
        .unwrap();
    }
    Train {
        _root: root,
        repo,
        db,
        bin,
        runs,
        snapshot,
        landed,
        heads,
    }
}

impl Train {
    fn cli(&self, args: &[&str]) -> String {
        output(
            Command::new(env!("CARGO_BIN_EXE_plangraph"))
                .current_dir(&self.repo)
                .env("HOME", &self.repo)
                .env("PATH", &self.bin)
                .env_remove("PLANGRAPH_ENV_FILE")
                .env("PLANGRAPH_LANDING_DB", &self.db)
                .env("PLANGRAPH_THROUGHPUT_RUNS", &self.runs)
                .args(args),
        )
    }

    fn collect(&self, until: &str) {
        self.collect_window("2026-11-21T00:00:00Z", until);
    }

    fn collect_window(&self, from: &str, until: &str) {
        self.cli(&[
            "throughput",
            "collect",
            "fixture/repo",
            "fixture",
            "HEAD",
            self.snapshot.to_str().unwrap(),
            from,
            until,
        ]);
    }

    fn report(&self) -> Value {
        serde_json::from_str(&self.cli(&["throughput", "report", "fixture/repo"])).unwrap()
    }
}

/// The report row of the six-part train (the fixture has an earlier landing too).
fn train_row(report: &Value) -> &Value {
    report["trains"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["train"] == "t")
        .unwrap()
}

fn six_parts_are_joined(mode: Mode) {
    let train = train(mode);
    train.collect("2026-11-22T00:00:00Z");
    let connection = Connection::open(&train.db).unwrap();
    let rows: Vec<(String, i64, i64)> = connection
        .prepare(
            "SELECT head,commits,seconds FROM throughput_accept_part WHERE sha=?1 ORDER BY head",
        )
        .unwrap()
        .query_map([&train.landed], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    // The wait ends at the landing's own time, whatever the commit times of the parts' merges.
    let wait = 10800;
    let mut expected: Vec<_> = train.heads.iter().map(|h| (h.clone(), 2, wait)).collect();
    expected.sort();
    assert_eq!(rows, expected, "one row and two commits per part head");
    let report = train.report();
    let row = train_row(&report);
    assert_eq!(row["accepted_parts"], 6, "{report}");
    assert_eq!(row["parts_complete"], 1, "{report}");
    assert_eq!(row["attempts"], 3, "1 + ci_reruns: {report}");
    assert_eq!(row["ci_reruns"], 2, "{report}");
    assert_eq!(row["drops"], 1, "{report}");
    assert_eq!(row["fix_tasks"], 3, "{report}");
    assert_eq!(row["minutes_per_merged_batch"], 10.0, "{report}");
    let hours = report["hours"].as_array().unwrap();
    let noon = hours
        .iter()
        .find(|h| h["hour"] == "2026-11-21T12:00:00Z")
        .unwrap();
    assert_eq!(noon["batches_merged"], 6, "{report}");
    assert_eq!(noon["items_landed"], 0, "{report}");
    assert_eq!(
        hours
            .iter()
            .map(|h| h["batches_merged"].as_u64().unwrap())
            .sum::<u64>(),
        7,
        "the train's six parts in one hour and the earlier landing's one"
    );
    assert_eq!(
        report["models"][0]["tokens_per_landed_commit"], 50.0,
        "{report}"
    );
}

#[test]
fn a_train_of_six_parts_joins_six_part_heads_in_pr_merge_mode() {
    six_parts_are_joined(Mode::PrMerge);
}

#[test]
fn a_train_of_six_parts_joins_six_part_heads_in_tested_fast_forward_mode() {
    // The part merges arrive on main under their own SHAs, before the landed head: the report still joins all six.
    six_parts_are_joined(Mode::TestedFastForward);
}

#[test]
fn a_landing_after_a_partial_hour_cutoff_is_not_counted_in_that_hour() {
    let train = train(Mode::PrMerge);
    let connection = Connection::open(&train.db).unwrap();
    // A second landing at 12:45 and an item landed after the cutoff.
    connection
        .execute(
            "INSERT INTO landing VALUES('fixture/repo','late','t2','[7]','2026-11-21T12:30:00Z','2026-11-21T12:45:00Z',0,0,0,'tested-ff')",
            [],
        )
        .unwrap();
    train.collect("2026-11-21T12:30:00Z");
    let report = train.report();
    let hours = report["hours"].as_array().unwrap();
    let noon = hours
        .iter()
        .find(|h| h["hour"] == "2026-11-21T12:00:00Z")
        .unwrap();
    assert_eq!(
        noon["batches_merged"], 6,
        "the 12:45 landing is after the 12:30 cutoff: {report}"
    );
    assert_eq!(
        report["trains"].as_array().unwrap().len(),
        2,
        "the 12:45 landing is not collected: {report}"
    );
}

const LEGACY: &str = "
CREATE TABLE throughput_hourly (repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL,
    commits INTEGER NOT NULL, items_closed INTEGER NOT NULL, PRIMARY KEY(repo,bucket));
CREATE TABLE throughput_daily (repo TEXT NOT NULL, day TEXT NOT NULL, observed_seconds INTEGER NOT NULL,
    commits INTEGER NOT NULL, items_closed INTEGER NOT NULL, PRIMARY KEY(repo,day));
CREATE TABLE throughput_item_landing (repo TEXT NOT NULL, item TEXT NOT NULL, landed INTEGER NOT NULL,
    PRIMARY KEY(repo,item));
CREATE TABLE throughput_writer_hourly (repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL,
    host TEXT NOT NULL, running INTEGER NOT NULL, measured INTEGER NOT NULL, active INTEGER NOT NULL,
    PRIMARY KEY(repo,bucket,host));
CREATE TABLE throughput_quota_hourly (repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL,
    provider TEXT NOT NULL, remaining REAL NOT NULL, PRIMARY KEY(repo,bucket,provider));
CREATE TABLE throughput_accept (repo TEXT NOT NULL, sha TEXT NOT NULL, landed INTEGER NOT NULL,
    seconds INTEGER NOT NULL, model TEXT, tokens INTEGER, commits INTEGER NOT NULL, PRIMARY KEY(repo,sha));
INSERT INTO throughput_hourly VALUES('fixture/repo',1763683200,1763686800,1,0);
INSERT INTO throughput_daily VALUES('fixture/repo','2025-11-21',3600,1,0);
INSERT INTO throughput_quota_hourly VALUES('fixture/repo',1763683200,1763686800,'codex',42.0);
INSERT INTO throughput_accept VALUES('fixture/repo','oldsha',1763683300,900,'old-model',500,5);
";

#[test]
fn a_database_of_the_first_collector_reports_its_history_before_and_after_an_upgrade() {
    let train = train(Mode::PrMerge);
    Connection::open(&train.db)
        .unwrap()
        .execute_batch(LEGACY)
        .unwrap();
    let history = |report: &Value| {
        let quota = report["quota"].as_array().unwrap();
        let q = quota.iter().find(|r| r["provider"] == "codex").unwrap();
        assert_eq!(q["quota_headroom_percent"], 42.0, "{report}");
        let model = report["models"]
            .as_array()
            .unwrap()
            .iter()
            .find(|m| m["model"] == "old-model")
            .unwrap();
        assert_eq!(model["tokens_per_landed_commit"], 100.0, "{report}");
        assert_eq!(
            report["days"][0]["accept_to_main_seconds"], 900.0,
            "{report}"
        );
    };
    // Reporting before any collection with the new binary works from the old tables.
    history(&train.report());
    // After collection the old samples are carried into the new tables, not lost.
    train.collect("2026-11-22T00:00:00Z");
    let report = train.report();
    let quota = report["quota"].as_array().unwrap();
    assert!(
        quota
            .iter()
            .any(|r| r["provider"] == "codex" && r["quota_headroom_percent"] == 42.0),
        "{report}"
    );
    let models = report["models"].as_array().unwrap();
    assert!(models.iter().any(|m| m["model"] == "old-model"), "{report}");
    assert!(
        models.iter().any(|m| m["model"] == "fixture-model"),
        "{report}"
    );
}

#[test]
fn recollecting_a_landing_does_not_count_a_migrated_legacy_acceptance_twice() {
    let train = train(Mode::PrMerge);
    let connection = Connection::open(&train.db).unwrap();
    connection.execute_batch(LEGACY).unwrap();
    // The first collector counted the landing once, under its landed SHA.
    connection
        .execute(
            "INSERT INTO throughput_accept VALUES('fixture/repo',?1,1763683300,900,'old-model',500,12)",
            [&train.landed],
        )
        .unwrap();
    train.collect("2026-11-22T00:00:00Z");
    train.collect("2026-11-22T00:00:00Z");
    let per_landing: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM throughput_accept_part WHERE sha=?1",
            [&train.landed],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        per_landing, 6,
        "six verified parts replace the legacy sample"
    );
    let report = train.report();
    assert_eq!(train_row(&report)["accepted_parts"], 6, "{report}");
    let commits: u64 = report["models"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|m| m["model"] == "fixture-model")
        .map(|m| m["landed_commits"].as_u64().unwrap())
        .sum();
    assert_eq!(commits, 12, "{report}");
}

#[test]
fn a_part_that_arrives_on_its_own_does_not_displace_its_trains_row() {
    let train = train(Mode::TestedFastForward);
    train.collect("2026-11-22T00:00:00Z");
    // The earlier window is collected again: the first-parent arrivals of the parts see the same heads.
    train.collect("2026-11-21T11:00:00Z");
    let connection = Connection::open(&train.db).unwrap();
    let on_landing: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM throughput_accept_part WHERE sha=?1",
            [&train.landed],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(on_landing, 6);
}

#[test]
fn collecting_an_unrelated_window_does_not_bring_a_retired_legacy_sample_back() {
    let train = train(Mode::PrMerge);
    let connection = Connection::open(&train.db).unwrap();
    connection.execute_batch(LEGACY).unwrap();
    connection
        .execute(
            "INSERT INTO throughput_accept VALUES('fixture/repo',?1,1763683300,900,'old-model',500,12)",
            [&train.landed],
        )
        .unwrap();
    train.collect("2026-11-22T00:00:00Z");
    // A window with no landing at all, collected afterwards.
    train.collect_window("2026-11-23T00:00:00Z", "2026-11-24T00:00:00Z");
    let rows: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM throughput_accept_part WHERE sha=?1",
            [&train.landed],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(rows, 6, "the retired legacy row stayed retired");
    let legacy: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM throughput_accept_part WHERE legacy=1 AND sha=?1",
            [&train.landed],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(legacy, 0);
}

#[test]
fn a_landing_with_no_earlier_landing_has_no_verified_base_and_its_ff_train_is_flagged_incomplete() {
    let train = train(Mode::TestedFastForward);
    // Remove the earlier landing: nothing bounds the fast-forwarded train from below.
    Connection::open(&train.db)
        .unwrap()
        .execute("DELETE FROM landing WHERE train='t0'", [])
        .unwrap();
    train.collect("2026-11-22T00:00:00Z");
    let report = train.report();
    // The one part that arrived under the landed SHA is reported, and the train is flagged as not verified.
    assert_eq!(train_row(&report)["accepted_parts"], 1, "{report}");
    assert_eq!(train_row(&report)["parts_complete"], 0, "{report}");
}

#[test]
fn a_landing_takes_only_the_parts_after_the_latest_landing_in_its_ancestry() {
    let train = train(Mode::PrMerge);
    // An intervening landing whose clock is later than the train's: the base is still the one in the ancestry.
    let connection = Connection::open(&train.db).unwrap();
    git(
        &train.repo,
        &["checkout", "-q", "-b", "other", &train.heads[0]],
    );
    output(dated(&train.repo, "2026-11-21T10:00:00Z").args([
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "unrelated",
    ]));
    let unrelated = git(&train.repo, &["rev-parse", "HEAD"]).trim().to_owned();
    git(&train.repo, &["checkout", "-q", "main"]);
    // Not an ancestor of main: it must never be taken for a base or a landing of this history.
    connection
        .execute(
            "INSERT INTO landing VALUES('fixture/repo',?1,'t9','[9]','2026-11-21T08:00:00Z','2026-11-21T11:30:00Z',0,0,0,'tested-ff')",
            [&unrelated],
        )
        .unwrap();
    train.collect("2026-11-22T00:00:00Z");
    let report = train.report();
    assert_eq!(train_row(&report)["accepted_parts"], 6, "{report}");
    assert_eq!(
        report["trains"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|t| t["train"] == "t9")
            .count(),
        1,
        "listed as a landing: {report}"
    );
    let on_unrelated: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM throughput_accept_part WHERE sha=?1",
            [&unrelated],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        on_unrelated, 0,
        "a landing off the selected main ref joins nothing"
    );
}

#[test]
fn an_accepted_head_that_reached_main_on_its_own_is_not_a_part_of_the_next_merge_train() {
    let train = train_with(Mode::PrMerge, true, &[]);
    train.collect("2026-11-22T00:00:00Z");
    let report = train.report();
    assert_eq!(
        train_row(&report)["accepted_parts"],
        6,
        "the merge's own base excludes it: {report}"
    );
    assert_eq!(train_row(&report)["parts_complete"], 1, "{report}");
}

#[test]
fn a_fast_forward_range_holding_more_accepted_heads_than_parts_stays_unverified() {
    let train = train_with(Mode::TestedFastForward, true, &[]);
    train.collect("2026-11-22T00:00:00Z");
    let report = train.report();
    // Seven accepted heads lie between the earlier landing and this one, the lander recorded six parts.
    assert_eq!(train_row(&report)["parts_complete"], 0, "{report}");
    assert!(
        train_row(&report)["accepted_parts"].as_u64().unwrap_or(0) < 6,
        "{report}"
    );
}

#[test]
fn a_stored_wait_survives_a_collection_of_an_unrelated_window() {
    let train = train(Mode::PrMerge);
    let connection = Connection::open(&train.db).unwrap();
    connection.execute_batch(LEGACY).unwrap();
    // A landing the new collector cannot join (its commit is not in the repository) with a stored 900 s wait.
    connection
        .execute(
            "INSERT INTO landing VALUES('fixture/repo','1111111111111111111111111111111111111111','old','[1]','2026-11-21T03:00:00Z','2026-11-21T04:00:00Z',0,0,0,'tested-ff')",
            [],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO throughput_accept VALUES('fixture/repo','1111111111111111111111111111111111111111',1763697600,900,'old-model',500,5)",
            [],
        )
        .unwrap();
    train.collect("2026-11-22T00:00:00Z");
    train.collect_window("2026-11-23T00:00:00Z", "2026-11-24T00:00:00Z");
    let report = train.report();
    let old = report["trains"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["train"] == "old")
        .unwrap();
    assert_eq!(old["accepted_parts"], 1, "{report}");
    assert_eq!(old["accept_to_main_seconds"], 900.0, "{report}");
    assert_eq!(old["parts_complete"], 0, "{report}");
}

#[test]
fn a_part_without_a_review_record_leaves_the_train_incomplete_and_unattributed_to_others() {
    let train = train_with(Mode::TestedFastForward, false, &[2]);
    train.collect("2026-11-22T00:00:00Z");
    let report = train.report();
    // Five of the six part heads have their review: five rows, and the train is not complete.
    assert_eq!(train_row(&report)["accepted_parts"], 5, "{report}");
    assert_eq!(train_row(&report)["parts_complete"], 0, "{report}");
}

#[test]
fn a_train_with_no_review_record_at_all_is_never_complete() {
    for mode in [Mode::PrMerge, Mode::TestedFastForward] {
        let train = train_with(mode, false, &[0, 1, 2, 3, 4, 5]);
        train.collect("2026-11-22T00:00:00Z");
        let report = train.report();
        assert_eq!(train_row(&report)["parts_complete"], 0, "{report}");
        assert!(train_row(&report)["accepted_parts"].is_null(), "{report}");
    }
}
