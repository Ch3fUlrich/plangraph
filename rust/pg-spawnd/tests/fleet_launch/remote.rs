use super::sandbox::{Launcher, Sandbox, LOCAL, PROMPT, REMOTE, SID};
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::os::unix::fs::symlink;
use std::process::{Command, Output, Stdio};

const DRY: &str = "forwarded dry run\n";
const STARTED: &str = "remote stdout\nfleet-launch: started 12345678-1234-1234-1234-123456789abc\n";

fn placement(s: &Sandbox) {
    s.hosts(json!([
        {"name":LOCAL, "memory_floor_gib":0, "last_resort":true},
        {"name":"fixture-other", "ssh":"fixture-other", "memory_floor_gib":4},
        {"name":REMOTE, "ssh":"fixture-hop", "shell":"bash", "memory_source":"windows", "memory_floor_gib":12},
    ]));
    fs::write(s.path("memory-fixture-other"), "MemAvailable: 8388608 kB\n").unwrap();
    fs::write(s.path("memory-fixture-hop"), "MemAvailable: 33554432 kB\n").unwrap();
    fs::write(s.path("windows-kb"), "20971520\n").unwrap();
    fs::write(s.path("ssh.stdout"), DRY).unwrap();
}

pub fn check(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    placement(&s);
    let out = s.launch("writer").arg("--dry-run").output().unwrap();
    s.expect(&out, 0, DRY, "");
    let hop = s.json("ssh.json");
    let argv = hop["argv"].as_array().unwrap();
    assert_eq!(
        &argv[..5],
        &json!([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "fixture-hop"
        ])
        .as_array()
        .unwrap()[..]
    );
    assert_eq!(argv.len(), 6);
    let command = argv[5].as_str().unwrap();
    assert!(command.starts_with("bash -lc 'python3 -c "));
    assert!(command.contains("import base64,sys;exec(base64.b64decode(sys.argv[1]))"));
    let payload = hop["payload"].as_object().unwrap();
    let expected = include_str!("../fixtures/fleet-launch/remote-dry-run.json")
        .replace("$ROOT", s.path("").to_str().unwrap().trim_end_matches('/'));
    assert_eq!(
        Value::Object(payload.clone()),
        serde_json::from_str::<Value>(&expected).unwrap()
    );
    s.no_launch();

    fs::write(s.path("windows-kb"), "12582912\n").unwrap();
    let out = s.launch("writer").arg("--dry-run").output().unwrap();
    s.expect(&out, 0, DRY, "");
    assert_eq!(s.json("ssh.json")["payload"]["host"], "fixture-other");
    s.no_launch();

    fs::write(s.path("windows-kb"), "11534336\n").unwrap();
    let out = s
        .launch("writer")
        .args(["--host", REMOTE, "--dry-run"])
        .output()
        .unwrap();
    s.expect(&out, 2, "", concat!(
        "skip fixture-remote: MemAvailable 11264 MB < floor 12288 MB\n",
        "fleet-launch: fixture-remote below memory floor: 11264 MB available, floor 12288 MB (no queue)\n",
    ));
    let out = s
        .launch("writer")
        .args(["--host", LOCAL, "--dry-run"])
        .output()
        .unwrap();
    s.expect(&out, 2, "", concat!(
        "skip fixture-remote: MemAvailable 11264 MB < floor 12288 MB\n",
        "fleet-launch: refused: writer is a worker role and fixture-other has room; fixture-local is the last resort. ",
        "Launch with --host fixture-other (worktree on that host), or set FLEET_LAST_RESORT_REASON=<why it must run here>\n",
    ));
    let out = s
        .launch("writer")
        .args(["--host", LOCAL, "--dry-run"])
        .env("FLEET_LAST_RESORT_REASON", "fixture")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    assert!(s
        .normalize(&out.stdout)
        .contains("--remote-control writer-0000"));
    s.no_launch();

    check_mirror(&s);
    check_selection_errors(launcher);
    check_destination(launcher);
}

fn check_mirror(s: &Sandbox) {
    fs::write(s.path("windows-kb"), "20971520\n").unwrap();
    let remote_spec = json!({
        "name":"writer", "session_name":"writer-0001", "seq":1,
        "session_id":SID, "host":"unexpected", "parent":"parent",
        "prompt_file":"remote-prompt.md"
    });
    fs::write(
        s.path("ssh.stdout"),
        format!("{STARTED}FLEET_REMOTE_SPEC={remote_spec}\n"),
    )
    .unwrap();
    fs::write(s.path("ssh.stderr"), "remote stderr\n").unwrap();
    let out = s
        .launch("writer")
        .args(["--host", REMOTE, "--parent", "parent"])
        .output()
        .unwrap();
    s.expect(&out, 0, STARTED, "remote stderr\n");
    assert_eq!(s.json("ssh.json")["payload"]["seq"], 1);
    assert_eq!(s.json("ssh.json")["payload"]["prompt"], PROMPT);
    let mut expected = remote_spec;
    expected["host"] = json!(REMOTE);
    assert_eq!(s.json("state/launch/writer.json"), expected);
    expected["prompt_file"] = json!(s.path("prompt"));
    assert_eq!(s.json("state/launch/sessions/writer-0001.json"), expected);
    assert_eq!(
        fs::read_to_string(s.path("state/launch/children/parent.txt")).unwrap(),
        "writer-0001\n"
    );
    assert!(!s.path("child.json").exists());

    fs::remove_file(s.path("prompt")).unwrap();
    fs::remove_file(s.path("ssh.json")).unwrap();
    let out = s
        .launch("writer")
        .args(["--host", REMOTE, "--dry-run"])
        .output()
        .unwrap();
    s.expect(&out, 2, "", "fleet-launch: host fixture-remote launch uncertain; do not retry: [Errno 2] No such file or directory: '$ROOT/prompt'\n");
    assert!(!s.path("ssh.json").exists());
}

fn check_selection_errors(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    s.hosts(json!([
        {"name":"fixture-no-floor"},
        {"name":"fixture-invalid", "memory_floor_gib":-1},
        {"name":REMOTE, "ssh":"fixture-hop", "memory_floor_gib":1},
    ]));
    fs::write(s.path("memory-fixture-hop"), "invalid\n").unwrap();
    let out = s.launch("review").arg("--dry-run").output().unwrap();
    s.expect(
        &out,
        2,
        "",
        concat!(
            "skip fixture-no-floor: no memory floor\n",
            "skip fixture-invalid: invalid memory floor\n",
            "skip fixture-remote: memory unknown\n",
            "fleet-launch: no host above memory floor (no queue)\n",
        ),
    );
    s.no_launch();

    placement(&s);
    let out = s.launch("routing").arg("--dry-run").output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    assert!(s.normalize(&out.stdout).starts_with("cd $ROOT/work && "));
    assert!(!s.path("ssh.json").exists());
    s.no_launch();
}

fn check_destination(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    placement(&s);
    let out = s
        .launch("writer")
        .args(["--host", REMOTE])
        .output()
        .unwrap();
    s.expect(&out, 0, DRY, "");
    let wire = s.json("ssh.json");
    let mut payload = wire["payload"].clone();
    assert_eq!(payload["seq"], 1);
    let command = wire["argv"][5].as_str().unwrap();
    let encoded = command
        .strip_suffix('\'')
        .unwrap()
        .rsplit_once(' ')
        .unwrap()
        .1;
    let target = s.path("target/code/plangraph-data/router/bin/fleet-launch");
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    symlink(&s.launcher, target).unwrap();

    let output = deliver(&s, encoded, &payload);
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        s.normalize(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    let stdout = s.normalize(&output.stdout);
    let (session, record) = stdout.split_once("FLEET_REMOTE_SPEC=").unwrap();
    assert_eq!(
        session,
        format!("{SID}\nfleet-launch: session writer-0001\nfleet-launch: started {SID}\n")
    );
    let record: Value = serde_json::from_str(record).unwrap();
    assert_eq!(record["host"], REMOTE);
    assert_eq!(record["seq"], 1);
    assert_eq!(
        record["prompt_file"],
        "$ROOT/target/.local/state/routing/launch/writer.remote-prompt.md"
    );
    assert_eq!(
        fs::read_to_string(s.path("target/.local/state/routing/launch/writer.remote-prompt.md"))
            .unwrap(),
        PROMPT
    );
    assert_eq!(
        fs::read_to_string(s.path("target/.local/state/routing/launch/writer.remote-mcp.json"))
            .unwrap(),
        "{}\n"
    );
    assert_eq!(s.json("child.json")["forwarded"], Value::Null);

    payload["argv"]
        .as_array_mut()
        .unwrap()
        .push(json!("--respawn"));
    let output = deliver(&s, encoded, &payload);
    s.expect(
        &output,
        2,
        "",
        "fleet-launch: refused host fixture-remote: missing launch spec; no launch\n",
    );
}

fn deliver(s: &Sandbox, encoded: &str, payload: &Value) -> Output {
    let mut command = Command::new("/usr/bin/timeout");
    s.isolate(&mut command);
    let mut child = command
        .args([
            "--kill-after=2s",
            "30s",
            "/usr/bin/python3",
            "-c",
            "import base64,sys;exec(base64.b64decode(sys.argv[1]))",
            encoded,
        ])
        .env("HOME", s.path("target"))
        .env("ROUTING_STATE_DIR", s.path("target/.local/state/routing"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(payload.to_string().as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}
