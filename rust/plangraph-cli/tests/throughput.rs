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
            "created_at":"2026-11-21T11:00:00Z","remaining_percentage":70,
            "next_reset_at":"2026-11-22T00:00:00Z"
        },{
            "provider":"fixture:other","window_key":"weekly",
            "created_at":"2026-11-21T11:30:00Z","remaining_percentage":40
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
    let quota = report["quota"].as_array().unwrap();
    let row = |account: &str| quota.iter().find(|r| r["account"] == account).unwrap();
    assert_eq!(row("sample")["quota_headroom_percent"], 70.0);
    assert_eq!(row("sample")["window"], "daily");
    assert_eq!(row("sample")["reset_at"], "2026-11-22T00:00:00Z");
    assert_eq!(row("other")["quota_headroom_percent"], 40.0);
    assert!(
        row("other")["reset_at"].is_null(),
        "an unnamed reset stays unknown"
    );
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

fn dated(repo: &Path, date: &str) -> Command {
    let mut command = git_command(repo);
    command
        .env("GIT_AUTHOR_DATE", date)
        .env("GIT_COMMITTER_DATE", date);
    command
}

#[test]
fn a_train_of_six_parts_joins_six_part_heads() {
    let root = tempfile::tempdir().unwrap();
    let repo = root.path();
    let bin = repo.join("bin");
    fs::create_dir(&bin).unwrap();
    let git_path = std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|dir| dir.join("git"))
        .find(|path| path.is_file())
        .expect("git on PATH");
    std::os::unix::fs::symlink(git_path.canonicalize().unwrap(), bin.join("git")).unwrap();
    git(repo, &["init", "-q", "-b", "main"]);
    let at = "2026-11-21T10:00:00Z";
    output(dated(repo, at).args(["commit", "-q", "--allow-empty", "-m", "base"]));
    git(repo, &["checkout", "-q", "-b", "train"]);
    let mut heads = Vec::new();
    for part in 1..=6 {
        git(
            repo,
            &["checkout", "-q", "-b", &format!("part{part}"), "main"],
        );
        for step in 1..=2 {
            output(dated(repo, at).args([
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                &format!("part {part} step {step}"),
            ]));
        }
        heads.push(git(repo, &["rev-parse", "HEAD"]).trim().to_owned());
        git(repo, &["checkout", "-q", "train"]);
        output(dated(repo, at).args([
            "merge",
            "-q",
            "--no-ff",
            "-m",
            &format!("train takes {part}"),
            &format!("part{part}"),
        ]));
    }
    git(repo, &["checkout", "-q", "main"]);
    output(dated(repo, "2026-11-21T12:00:00Z").args([
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "land train",
        "train",
    ]));
    let landed = git(repo, &["rev-parse", "HEAD"]).trim().to_owned();
    let db = repo.join("landing.sqlite3");
    let connection = Connection::open(&db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE landing(repo TEXT,landed_sha TEXT,train TEXT,parts TEXT,
         build_started_at TEXT,landed_at TEXT,PRIMARY KEY(repo,landed_sha));",
        )
        .unwrap();
    connection.execute(
        "INSERT INTO landing VALUES('fixture/repo',?1,'t','[1,2,3,4,5,6]','2026-11-21T11:00:00Z','2026-11-21T12:00:00Z')",
        [&landed],
    ).unwrap();
    let snapshot = repo.join("keeper.json");
    fs::write(&snapshot, json!({"items":[]}).to_string()).unwrap();
    let runs = repo.join("runs");
    for (n, head) in heads.iter().enumerate() {
        let dir = runs.join(format!("run-{n}"));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("record.json"), json!({
            "run_id":format!("run-{n}"),"run_kind":"native-codex","host":"fixture","status":"completed",
            "started":"2026-11-21T10:00:00Z","ended":"2026-11-21T11:00:00Z","final_head":head,
            "review":{"verdict":"ACCEPT","head":head,"accepted_at":"2026-11-21T11:00:00Z"}
        }).to_string()).unwrap();
        fs::write(
            dir.join("ledger.jsonl"),
            "{\"model\":\"fixture-model\",\"tokens\":100}\n",
        )
        .unwrap();
    }
    let cli = |args: &[&str]| {
        output(
            Command::new(env!("CARGO_BIN_EXE_plangraph"))
                .current_dir(repo)
                .env("HOME", repo)
                .env("PATH", &bin)
                .env_remove("PLANGRAPH_ENV_FILE")
                .env("PLANGRAPH_LANDING_DB", &db)
                .env("PLANGRAPH_THROUGHPUT_RUNS", &runs)
                .args(args),
        )
    };
    cli(&[
        "throughput",
        "collect",
        "fixture/repo",
        "fixture",
        "HEAD",
        snapshot.to_str().unwrap(),
        "2026-11-21T00:00:00Z",
        "2026-11-22T00:00:00Z",
    ]);
    let rows: Vec<(String, i64, i64)> = connection
        .prepare(
            "SELECT head,commits,seconds FROM throughput_accept_part WHERE sha=?1 ORDER BY head",
        )
        .unwrap()
        .query_map([&landed], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    let mut expected: Vec<_> = heads.iter().map(|h| (h.clone(), 2, 3600)).collect();
    expected.sort();
    assert_eq!(rows, expected);
    let report: Value =
        serde_json::from_str(&cli(&["throughput", "report", "fixture/repo"])).unwrap();
    assert_eq!(report["trains"][0]["accepted_parts"], 6, "{report}");
    assert_eq!(
        report["models"][0]["tokens_per_landed_commit"], 50.0,
        "{report}"
    );
}
