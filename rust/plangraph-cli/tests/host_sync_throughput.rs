#![cfg(unix)]

use chrono::{DateTime, Utc};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::{collections::BTreeMap, fs, path::Path, process::Command};

fn output(command: &mut Command) -> String {
    let result = command.output().unwrap();
    assert!(result.status.success(), "{result:?}");
    String::from_utf8(result.stdout).unwrap()
}

fn git(repo: &Path) -> Command {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    command
        .current_dir(repo)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid");
    command.args(["-c", "commit.gpgsign=false"]);
    command
}

#[test]
fn throughput_environment_feeds_the_service_tick_on_a_fixture() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("fixture home");
    let origin = temp.path().join("fixture/repository");
    fs::create_dir_all(&origin).unwrap();
    let repo = home.join("code/plangraph");
    fs::create_dir_all(repo.parent().unwrap()).unwrap();
    let end = Utc::now().timestamp().div_euclid(3600) * 3600;
    let stamp = |offset: i64| {
        DateTime::<Utc>::from_timestamp(end + offset * 3600, 0)
            .unwrap()
            .to_rfc3339()
    };
    output(git(&origin).args(["init", "-q", "-b", "main"]));
    output(
        git(&origin)
            .env("GIT_AUTHOR_DATE", stamp(-2))
            .env("GIT_COMMITTER_DATE", stamp(-2))
            .args(["commit", "-qm", "Fixture landing", "--allow-empty"]),
    );
    output(
        git(temp.path())
            .args(["clone", "-q"])
            .arg(&origin)
            .arg(&repo),
    );

    let state = home.join(".local/share/plangraph");
    fs::create_dir_all(&state).unwrap();
    let snapshot = state.join("plan.snapshot.json");
    fs::write(
        &snapshot,
        json!({"items":[
            {"project":"plangraph","key":"plangraph:FIXTURE","state":"done","done_at":stamp(-2)}
        ]})
        .to_string(),
    )
    .unwrap();
    let conn = Connection::open(state.join("landing-telemetry.sqlite3")).unwrap();
    conn.execute_batch(
        "CREATE TABLE landing(repo TEXT,landed_sha TEXT,train TEXT,parts TEXT,
         build_started_at TEXT,landed_at TEXT,PRIMARY KEY(repo,landed_sha));",
    )
    .unwrap();

    output(
        Command::new(env!("CARGO_BIN_EXE_host-sync"))
            .env("HOME", &home)
            .args(["--install-env", "throughput"]),
    );
    let env = fs::read_to_string(home.join(".config/plangraph/throughput.env")).unwrap();
    let values = env
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key, value.trim_matches('"')))
        .collect::<BTreeMap<_, _>>();
    assert_eq!(
        values,
        BTreeMap::from([
            ("PLANGRAPH_THROUGHPUT_REPO", "fixture/repository"),
            ("PLANGRAPH_THROUGHPUT_PROJECT", "plangraph"),
            ("PLANGRAPH_THROUGHPUT_SNAPSHOT", snapshot.to_str().unwrap()),
        ])
    );

    let unit = include_str!("../../../deploy/systemd/pg-throughput.service");
    assert!(unit.contains("EnvironmentFile=%h/.config/plangraph/throughput.env"));
    let workdir = unit
        .lines()
        .find_map(|line| line.strip_prefix("WorkingDirectory="))
        .unwrap();
    let exec = unit
        .lines()
        .find_map(|line| line.strip_prefix("ExecStart="))
        .unwrap();
    let mut args = exec.split_whitespace();
    let binary = args.next().unwrap().replace("%h", home.to_str().unwrap());
    let path = Path::new(&binary);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(env!("CARGO_BIN_EXE_plangraph"), path).unwrap();
    let tick = output(
        Command::new(path)
            .current_dir(workdir.replace("%h", home.to_str().unwrap()))
            .env("HOME", &home)
            .env_remove("XDG_DATA_HOME")
            .env_remove("PLANGRAPH_LANDING_DB")
            .env("PLANGRAPH_THROUGHPUT_START", stamp(-3))
            .args(args.map(|arg| {
                arg.strip_prefix("${")
                    .and_then(|arg| arg.strip_suffix('}'))
                    .map_or(arg, |key| values[key])
            })),
    );
    assert!(
        serde_json::from_str::<Value>(&tick).unwrap()["hours"]
            .as_u64()
            .unwrap()
            >= 3
    );
    let counts = conn.query_row(
        "SELECT SUM(commits), SUM(items_closed) FROM throughput_hourly WHERE repo = 'fixture/repository'",
        [], |row| Ok((row.get::<_, u64>(0)?, row.get::<_, u64>(1)?))).unwrap();
    assert_eq!(counts, (1, 1));
}
