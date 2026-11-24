#![cfg(target_os = "linux")]

use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn install(home: &Path, family: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .args(["--install-env", family])
        .output()
        .unwrap()
}

#[path = "host_sync_env/disk.rs"]
mod disk;

#[test]
fn keeper_backup_installs_only_empty_connection_keys_with_private_mode() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let output = install(home, "keeper-backup");
    assert!(output.status.success(), "{output:?}");
    let env = home.join(".config/plangraph/keeper-backup.env");
    let text = fs::read_to_string(&env).unwrap();
    let docker = include_str!("../../pg-keeper-backup/src/docker.rs");
    let keys = docker
        .split_once("pub const PG_ENV:")
        .unwrap()
        .1
        .split_once("= [")
        .unwrap()
        .1
        .split_once("];")
        .unwrap()
        .0;
    let expected: Vec<_> = keys
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| line.trim_end_matches(',').trim_matches('"'))
        .collect();
    let entries: Vec<_> = text
        .lines()
        .filter(|line| !line.starts_with('#'))
        .map(|line| line.split_once('=').unwrap())
        .collect();
    assert_eq!(
        entries,
        expected
            .into_iter()
            .map(|key| (key, ""))
            .collect::<Vec<_>>()
    );
    assert_eq!(
        fs::metadata(&env).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::write(&env, "# existing operator file\n").unwrap();
    assert!(install(home, "keeper-backup").status.success());
    assert_eq!(
        fs::read_to_string(env).unwrap(),
        "# existing operator file\n"
    );
}

#[test]
fn quota_alarms_discover_the_store_and_report_missing_candidates() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let db = home.join(".omniroute/storage.sqlite");
    let alternate = home.join(".local/share/omniroute/storage.sqlite");
    let target = home.join(".config/plangraph/quota-alarms.env");
    let missing = install(home, "quota-alarms");
    assert!(!missing.status.success());
    let message = String::from_utf8_lossy(&missing.stderr);
    for path in [&db, &alternate] {
        assert!(message.contains(path.to_str().unwrap()), "{message}");
    }
    assert!(!target.exists());

    fixture_quota_db(&db);
    let output = install(home, "quota-alarms");
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        fs::read_to_string(target).unwrap(),
        format!("QUOTA_SNAPSHOT_DB=\"{}\"\n", db.display())
    );
}

fn fixture_quota_db(path: &Path) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    rusqlite::Connection::open(path)
        .unwrap()
        .execute_batch(
            "CREATE TABLE quota_snapshots(id INTEGER, provider TEXT, connection_id TEXT,
            window_key TEXT, remaining_percentage REAL, is_exhausted INTEGER,
            next_reset_at TEXT, created_at TEXT)",
        )
        .unwrap();
}

#[test]
fn quota_alarms_uses_gateway_mount_before_a_cli_store() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    fixture_quota_db(&home.join(".omniroute/storage.sqlite"));
    let mount = home.join("gateway mount");
    let database = mount.join("storage.sqlite");
    fixture_quota_db(&database);
    let docker = home.join("docker");
    pg_worker_core::executable_test_support::write_executable(
        &docker,
        "#!/bin/sh\n[ \"$1\" = inspect ] || exit 1\nprintf '%s\\n' \"$FIXTURE_MOUNTS\"\n",
    );
    let output = Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .env("PATH", home)
        .env("QUOTA_GATEWAY_CONTAINER", "fixture")
        .env(
            "FIXTURE_MOUNTS",
            serde_json::json!([{"Source": mount, "Destination": "/app/data"}]).to_string(),
        )
        .args(["--install-env", "quota-alarms"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        fs::read_to_string(home.join(".config/plangraph/quota-alarms.env")).unwrap(),
        format!("QUOTA_SNAPSHOT_DB=\"{}\"\n", database.display())
    );
}

#[test]
fn state_shadow_stages_both_instances_and_leaves_shared_profiles_alone() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let shared = home.join("fleet-settings.json");
    let unit = include_str!("../../../deploy/systemd/plangraph-state-shadow@.service");
    let bin = home.join(".local/bin");
    fs::create_dir_all(&bin).unwrap();
    std::os::unix::fs::symlink(env!("CARGO_BIN_EXE_plangraph"), bin.join("plangraph")).unwrap();
    std::os::unix::fs::symlink("/bin/true", bin.join("turn-budget-hook")).unwrap();
    fs::write(
        &shared,
        r#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"existing"}]}]}}"#,
    )
    .unwrap();
    let run = |extra: &[&Path]| {
        Command::new(env!("CARGO_BIN_EXE_host-sync"))
            .env_clear()
            .env("HOME", home)
            .args(["--install-env", "state-shadow"])
            .args(extra)
            .output()
            .unwrap()
    };
    for _ in 0..2 {
        let output = run(&[]);
        assert!(output.status.success(), "{output:?}");
    }
    // The old form named the profiles to edit; a shared profile must never receive the hook.
    assert!(!run(&[shared.as_path()]).status.success());
    assert!(!fs::read_to_string(&shared)
        .unwrap()
        .contains("measure-only"));
    let config = home.join(".config/plangraph/state-shadow");
    for role in ["plan-order", "routing"] {
        let content = fs::read_to_string(config.join(format!("{role}.env"))).unwrap();
        for (key, suffix) in [
            ("PG_TOKEN_FILE", "token"),
            ("PLANGRAPH_STATE_CONFIG", "json"),
        ] {
            assert!(content.contains(&format!(
                "{key}=\"{}\"\n",
                config.join(format!("{role}.{suffix}")).display()
            )));
        }
        run_shadow_once(home, role, unit, &content);
        if Path::new("/usr/bin/systemd-analyze").is_file() {
            let service = temp
                .path()
                .join(format!("plangraph-state-shadow@{role}.service"));
            fs::write(&service, unit.replace("%h", home.to_str().unwrap())).unwrap();
            let verify = Command::new("/usr/bin/systemd-analyze")
                .arg("verify")
                .arg(service)
                .output()
                .unwrap();
            assert!(verify.status.success(), "{verify:?}");
        }
    }
}

fn run_shadow_once(home: &Path, role: &str, unit: &str, environment: &str) {
    use serde_json::json;
    use std::{
        io::{BufRead, BufReader, ErrorKind, Write},
        net::TcpListener,
        sync::mpsc,
        time::Duration,
    };

    let state = home.join("shadow-state");
    let config = home.join(".config/plangraph/state-shadow");
    fs::write(
        config.join(format!("{role}.token")),
        format!("{role}-fixture"),
    )
    .unwrap();
    fs::write(
        config.join(format!("{role}.json")),
        json!({
            "role": role, "principal": role, "directory": state, "inbox": home.join("inbox"),
            "transcript": home.join("session.jsonl"), "session_id": "session",
            "data_repo": home, "decision_base": "a".repeat(40), "github_repo": "example/fixture",
            "github_since": "2026-01-01T00:00:00Z", "quota": home.join("quota.json")
        })
        .to_string(),
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (stop, stopped) = mpsc::channel::<()>();
    let name = role.to_owned();
    let server = std::thread::spawn(move || loop {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let lines: Vec<_> = BufReader::new(&stream)
                    .lines()
                    .map(Result::unwrap)
                    .take_while(|line| !line.is_empty())
                    .collect();
                assert!(lines.iter().any(|line| line
                    .eq_ignore_ascii_case(&format!("Authorization: Bearer {name}-fixture"))));
                let body = json!({
                    "name": name, "role": "router", "projects": [], "rows": [], "next_cursor": 0,
                    "more": false, "items": [], "messages": [], "hosts": []
                })
                .to_string();
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            }
            Err(err) if err.kind() == ErrorKind::WouldBlock => {
                if stopped.recv_timeout(Duration::from_millis(5))
                    != Err(mpsc::RecvTimeoutError::Timeout)
                {
                    break;
                }
            }
            Err(err) => panic!("{err}"),
        }
    });

    let exec = unit
        .lines()
        .find_map(|line| line.strip_prefix("ExecStart="))
        .unwrap();
    let mut args = exec.split_whitespace();
    let binary = args.next().unwrap().replace("%h", home.to_str().unwrap());
    let output = Command::new(binary)
        .env_clear()
        .env("HOME", home)
        .env("PATH", home.join(".local/bin"))
        .env("PG_URL", url)
        .envs(
            environment
                .lines()
                .filter_map(|line| line.split_once('='))
                .map(|(key, value)| (key, value.trim_matches('"'))),
        )
        .args(args.map(|arg| if arg == "%i" { role } else { arg }))
        .arg("--once")
        .output()
        .unwrap();
    stop.send(()).unwrap();
    server.join().unwrap();
    assert!(output.status.success(), "{output:?}");
    assert!(state.join(role).join("digest-1.txt").is_file());
}

#[test]
fn state_shadow_generates_role_config_from_seed_and_launch_record_once() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let bin = home.join(".local/bin");
    fs::create_dir_all(&bin).unwrap();
    std::os::unix::fs::symlink("/bin/true", bin.join("turn-budget-hook")).unwrap();
    let config = home.join(".config/plangraph/state-shadow");
    fs::create_dir_all(&config).unwrap();
    let run = || {
        let output = Command::new(env!("CARGO_BIN_EXE_host-sync"))
            .env_clear()
            .env("HOME", home)
            .args(["--install-env", "state-shadow"])
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        String::from_utf8(output.stdout).unwrap()
    };
    // Without a seed and a launch record nothing is invented.
    assert!(run().contains("NEEDS"));
    assert!(!config.join("routing.json").exists());
    let projects = home.join(".claude/projects/-moved");
    fs::create_dir_all(&projects).unwrap();
    fs::write(projects.join("sid-7.jsonl"), "").unwrap();
    let launch = home.join(".local/state/routing/launch");
    fs::create_dir_all(&launch).unwrap();
    fs::write(
        launch.join("routing.json"),
        r#"{"session_id":"sid-7","cwd":"/work/tree"}"#,
    )
    .unwrap();
    fs::write(
        config.join("routing.seed.json"),
        r#"{"principal":"routing-reader","inbox":"/in","data_repo":"/d","decision_base":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","github_repo":"example/demo","github_since":"2026-01-01T00:00:00Z"}"#,
    )
    .unwrap();
    run();
    let made: serde_json::Value =
        serde_json::from_slice(&fs::read(config.join("routing.json")).unwrap()).unwrap();
    assert_eq!(made["role"], "routing");
    assert_eq!(made["principal"], "routing-reader");
    assert_eq!(made["session_id"], "sid-7");
    assert_eq!(
        made["transcript"],
        projects.join("sid-7.jsonl").to_str().unwrap()
    );
    assert_eq!(made["quota"], config.join("quota.json").to_str().unwrap());
    assert!(!config.join("plan-order.json").exists());
    // A second install keeps the file the launcher maintains.
    fs::write(config.join("routing.json"), "{\"kept\":true}").unwrap();
    assert!(run().contains("SAME"));
    assert_eq!(
        fs::read_to_string(config.join("routing.json")).unwrap(),
        "{\"kept\":true}"
    );
}
