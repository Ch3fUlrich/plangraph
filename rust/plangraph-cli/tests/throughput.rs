#![cfg(unix)]

use rusqlite::Connection;
use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

fn output(command: &mut Command) -> String {
    let result = command.output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    String::from_utf8(result.stdout).unwrap()
}

fn git(repo: &Path, args: &[&str]) -> String {
    output(git_command(repo).args(args))
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

#[test]
fn throughput_persists_both_baseline_days_in_landing_database() {
    let root = tempfile::tempdir().unwrap();
    let bin = root.path().join("bin");
    fs::create_dir(&bin).unwrap();
    let git_path = std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|dir| dir.join("git"))
        .find(|path| path.is_file())
        .expect("git on PATH");
    std::os::unix::fs::symlink(git_path.canonicalize().unwrap(), bin.join("git")).unwrap();
    git(root.path(), &["init", "-q", "-b", "main"]);
    for date in [
        "2026-11-19T12:00:00Z",
        "2026-11-20T12:00:00Z",
        "2026-11-21T12:00:00Z",
    ] {
        output(
            git_command(root.path())
                .env("GIT_AUTHOR_DATE", date)
                .env("GIT_COMMITTER_DATE", date)
                .args([
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    date,
                    "-m",
                    "Plan-Item: fixture:SHARED",
                ]),
        );
    }
    let day20 = git(root.path(), &["rev-parse", "HEAD^"]);
    let day19 = git(root.path(), &["rev-parse", "HEAD^^"]);
    let day21 = git(root.path(), &["rev-parse", "HEAD"]);
    let db = root.path().join("landing.sqlite3");
    let connection = Connection::open(&db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE landing(repo TEXT,landed_sha TEXT,train TEXT,parts TEXT,
         build_started_at TEXT,landed_at TEXT,PRIMARY KEY(repo,landed_sha));",
        )
        .unwrap();
    connection.execute(
        "INSERT INTO landing VALUES('fixture/repo',?1,'fixture','[1,2]','2026-11-21T11:00:00Z','2026-11-21T12:00:00Z')",
        [day21.trim()],
    ).unwrap();
    let snapshot = root.path().join("keeper.json");
    fs::write(&snapshot, json!({"items":[
        {"project":"fixture","key":"fixture:A","state":"done","done_at":"2026-11-20T15:00:00Z"},
        {"project":"fixture","key":"fixture:B","state":"done","done_at":"2026-11-21T16:00:00Z"},
        {"project":"different","key":"different:A","state":"done","done_at":"2026-11-21T16:00:00Z"}
    ]}).to_string()).unwrap();
    let runs = root.path().join("runs");
    let writer = runs.join("run-writer");
    fs::create_dir_all(&writer).unwrap();
    fs::write(
        writer.join("record.json"),
        json!({
            "run_id":"run-writer","run_kind":"native-codex","host":"fixture","status":"completed",
            "started":"2026-11-21T10:00:00Z","ended":"2026-11-21T12:00:00Z",
            "final_head":day21.trim(),
            "review":{"verdict":"ACCEPT","head":day21.trim(),"accepted_at":"2026-11-21T11:45:00Z"},
            "writer_turns":[{"started":"2026-11-21T10:00:00Z","ended":"2026-11-21T11:00:00Z"}]
        })
        .to_string(),
    )
    .unwrap();
    fs::write(
        writer.join("ledger.jsonl"),
        "{\"model\":\"fixture-model\",\"tokens\":1200}\n",
    )
    .unwrap();
    let quota = root.path().join("quota.json");
    fs::write(
        &quota,
        json!([{
            "provider":"fixture","connection_id":"sample","window_key":"daily",
            "created_at":"2026-11-21T11:00:00Z","remaining_percentage":70
        }])
        .to_string(),
    )
    .unwrap();
    let cli = || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_plangraph"));
        command
            .current_dir(root.path())
            .env("HOME", root.path())
            .env("PATH", &bin)
            .env_remove("PLANGRAPH_ENV_FILE")
            .env("PLANGRAPH_LANDING_DB", &db)
            .env("PLANGRAPH_THROUGHPUT_RUNS", &runs)
            .env("PLANGRAPH_THROUGHPUT_QUOTA", &quota);
        command
    };
    let collect = [
        "throughput",
        "collect",
        "fixture/repo",
        "fixture",
        "HEAD",
        snapshot.to_str().unwrap(),
        "2026-11-20T00:00:00Z",
        "2026-11-22T00:00:00Z",
    ];
    output(cli().args(collect));
    output(cli().args(collect));
    let mut shorter = collect;
    shorter[7] = "2026-11-21T17:30:00Z";
    output(cli().args(shorter));
    let report =
        output(
            cli()
                .env("PATH", root.path())
                .args(["throughput", "report", "fixture/repo"]),
        );
    let report: Value = serde_json::from_str(&report).unwrap();
    let days = report["days"].as_array().unwrap();
    assert_eq!(days.len(), 2, "{report}");
    let hand = git(
        root.path(),
        &[
            "rev-list",
            "--count",
            &format!("{}..{}", day19.trim(), day20.trim()),
        ],
    );
    assert_eq!(days[0]["commits"], hand.trim().parse::<u64>().unwrap());
    assert_eq!(days[0]["items_closed"], 1);
    assert_eq!(days[1]["items_closed"], 1);
    assert_eq!(days[0]["items_landed"], 1);
    assert_eq!(days[1]["items_landed"], 0);
    assert_eq!(days[1]["observed_seconds"], 86400);
    assert_eq!(days[1]["trains"], 1);
    assert_eq!(days[1]["train_parts"], 2);
    assert_eq!(days[1]["train_seconds"], 3600);
    assert_eq!(days[1]["accept_to_main_seconds"], 900.0);
    assert_eq!(report["writers"][1]["writer_active_share"], 0.5);
    assert_eq!(report["models"][0]["tokens_per_landed_commit"], 1200.0);
    assert_eq!(report["quota"][0]["quota_headroom_percent"], 70.0);
    let rows: (i64, i64) = connection
        .query_row(
            "SELECT (SELECT COUNT(*) FROM throughput_hourly), (SELECT COUNT(*) FROM landing)",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(rows, (48, 1));
    assert!(days[0]["accept_to_main_seconds"].is_null());
}

#[test]
fn throughput_report_does_not_create_a_missing_landing_database() {
    let root = tempfile::tempdir().unwrap();
    let db = root.path().join("missing.sqlite3");
    let result = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env("HOME", root.path())
        .env("PATH", root.path())
        .env_remove("PLANGRAPH_ENV_FILE")
        .env("PLANGRAPH_LANDING_DB", &db)
        .args(["throughput", "report", "fixture/repo"])
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stderr).contains("landing database unavailable"));
    assert!(!db.exists());
}
