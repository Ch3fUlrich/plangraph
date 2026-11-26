use super::sandbox::{Launcher, Sandbox, LOCAL, REMOTE, SID};
use serde_json::{json, Value};
use std::{fs, process::Output};

const NAME: &str = "writer-0042";
const ORIGIN_ID: &str = "98765432-1234-1234-1234-123456789abc";
const RECORD: &str = "launch/sessions/writer-0042.json";
const DEST_STATE: &str = "target/.local/state/routing";

fn record(s: &Sandbox, host: &str, id: &str) -> Value {
    json!({
        "name": "writer", "cwd": s.path("work"), "session_id": id,
        "session_name": NAME, "seq": 42, "host": host, "prompt_file": s.path("prompt"),
    })
}

fn remote_sandbox(launcher: &Launcher) -> Sandbox {
    let s = Sandbox::new(launcher);
    s.hosts(json!([
        {"name": LOCAL, "memory_floor_gib": 0},
        {"name": REMOTE, "ssh": "fixture-hop", "memory_floor_gib": 0},
    ]));
    fs::write(s.path("memory-fixture-hop"), "MemAvailable: 1024 kB\n").unwrap();
    fs::write(s.path("ssh.stdout"), "").unwrap();
    s
}

fn remote(s: &Sandbox, dry: bool) -> (String, Value) {
    let mut command = s.launch(NAME);
    command.args(["--respawn", "--host", REMOTE]);
    if dry {
        command.arg("--dry-run");
    }
    let out = command.output().unwrap();
    assert!(out.status.success(), "{}", s.normalize(&out.stderr));
    let wire = s.json("ssh.json");
    assert_eq!(wire["payload"]["spec"], json!({}));
    assert_eq!(wire["argv"][4], "fixture-hop");
    assert_eq!(wire["payload"]["host"], REMOTE);
    let bootstrap = wire["argv"][5].as_str().unwrap();
    let (_, encoded) = bootstrap
        .strip_suffix('\'')
        .unwrap()
        .rsplit_once(' ')
        .unwrap();
    (encoded.to_owned(), wire["payload"].clone())
}

fn assert_dry_run(s: &Sandbox, out: &Output) {
    assert!(
        out.status.success(),
        "status {}, stdout: {}, stderr: {}",
        out.status,
        s.normalize(&out.stdout),
        s.normalize(&out.stderr)
    );
    let command = s.normalize(&out.stdout);
    for arg in [
        format!("--resume {SID}"),
        format!("--remote-control {NAME}"),
        format!("-n {NAME}"),
    ] {
        assert!(command.contains(&arg), "missing {arg}: {command}");
    }
    assert!(!s.path("child.json").exists());
}

pub fn forwarded_respawn_uses_destination_record(launcher: &Launcher) {
    let s = remote_sandbox(launcher);
    s.write_json(&format!("{DEST_STATE}/{RECORD}"), record(&s, REMOTE, SID));
    let (encoded, payload) = remote(&s, true);
    s.install_fixture_receiver();
    assert_dry_run(&s, &s.deliver(&encoded, &payload));
    fs::remove_file(s.path("prompt")).unwrap();
    assert_dry_run(&s, &s.deliver(&encoded, &payload));
    assert!(!s.path("state").exists());
    assert!(!s.path(&format!("{DEST_STATE}/session-seq")).exists());

    fs::write(s.path("prompt"), super::sandbox::PROMPT).unwrap();
    s.write_json(&format!("state/{RECORD}"), record(&s, REMOTE, ORIGIN_ID));
    let (encoded, payload) = remote(&s, false);
    let out = s.deliver(&encoded, &payload);
    assert!(out.status.success(), "{}", s.normalize(&out.stderr));
    let stdout = s.normalize(&out.stdout);
    assert!(stdout.contains(&format!("fleet-launch: started {SID}")));
    let reported: Value =
        serde_json::from_str(stdout.split_once("FLEET_REMOTE_SPEC=").unwrap().1).unwrap();
    assert_eq!(reported["session_id"], SID);
    assert_eq!(reported["session_name"], NAME);
    assert_eq!(reported["host"], REMOTE);
    assert_resume_args(&s);
    assert!(!s.path(&format!("{DEST_STATE}/session-seq")).exists());

    let s = remote_sandbox(launcher);
    s.write_json(&format!("state/{RECORD}"), record(&s, REMOTE, SID));
    let out = s
        .launch("writer")
        .args([
            "--respawn",
            "--session",
            NAME,
            "--host",
            REMOTE,
            "--dry-run",
        ])
        .env("FLEET_LOCAL_HOST", REMOTE)
        .output()
        .unwrap();
    assert_dry_run(&s, &out);
    assert!(!s.path("ssh.json").exists());
}

pub fn source_only_record_is_refused(launcher: &Launcher) {
    let s = remote_sandbox(launcher);
    s.write_json(&format!("state/{RECORD}"), record(&s, REMOTE, ORIGIN_ID));
    s.install_fixture_receiver();
    for dry in [true, false] {
        let (encoded, payload) = remote(&s, dry);
        let out = s.deliver(&encoded, &payload);
        let stderr = s.normalize(&out.stderr);
        assert_eq!(out.status.code(), Some(2), "{stderr}");
        assert!(
            stderr.contains(&format!("missing saved session {NAME}")),
            "{stderr}"
        );
        assert!(out.stdout.is_empty());
        assert!(!s.path("child.json").exists());
        assert!(!s.path(&format!("{DEST_STATE}/{RECORD}")).exists());
        assert!(!stderr.contains(ORIGIN_ID));
    }
}

pub fn success_without_id_preserves_selected_id(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    s.write_json(&format!("state/{RECORD}"), record(&s, LOCAL, SID));
    fs::write(s.path("child.stdout"), "backgrounded no-id\n").unwrap();
    let out = s.launch(NAME).arg("--respawn").output().unwrap();
    assert!(out.status.success(), "{}", s.normalize(&out.stderr));
    assert_resume_args(&s);
    let snapshot = s.json(&format!("state/{RECORD}"));
    assert_eq!(snapshot["session_id"], SID);
    assert_eq!(snapshot["session_name"], NAME);
    assert_eq!(snapshot["seq"], 42);
    assert_eq!(snapshot, s.json("state/launch/writer.json"));
    assert!(!s.path("state/session-seq").exists());
    fs::remove_file(s.path("child.json")).unwrap();
    let next = s
        .launch(NAME)
        .args(["--respawn", "--dry-run"])
        .output()
        .unwrap();
    assert_dry_run(&s, &next);
}

fn assert_resume_args(s: &Sandbox) {
    let child = s.json("child.json");
    let argv = child["argv"].as_array().unwrap();
    for pair in [["--resume", SID], ["--remote-control", NAME], ["-n", NAME]] {
        assert!(argv.windows(2).any(|args| args == pair), "{argv:?}");
    }
}
