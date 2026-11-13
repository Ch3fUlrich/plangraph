mod common;

use common::python;
use pg_hook::{shadow, Env, Hook, HookError, HookOutput};
use serde_json::Value;
use std::path::Path;

struct Hex;
impl Hook for Hex {
    fn run(&self, input: &[u8], _: &Env) -> Result<HookOutput, HookError> {
        Ok(HookOutput {
            exit_code: 0,
            stdout: input.iter().map(|b| format!("{b:02x}")).collect(),
            stderr: String::new(),
        })
    }
}

fn record(log: &Path) -> Value {
    let text = std::fs::read_to_string(log).unwrap();
    assert_eq!(text.lines().count(), 1);
    serde_json::from_str(&text).unwrap()
}

#[test]
fn non_utf8_payload_is_identical_for_python_and_rust() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hex.py");
    std::fs::write(
        &script,
        "import sys\nsys.stdout.write(sys.stdin.buffer.read().hex())\n",
    )
    .unwrap();
    let log = tmp.path().join("shadow.jsonl");
    let output = shadow(
        "bash_guard",
        b"\xff\0{not json\n",
        &Env::new(),
        &mut python(&script),
        &Hex,
        &log,
    )
    .unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"ff007b6e6f74206a736f6e0a");
    let record = record(&log);
    assert_eq!(record["same"], true);
    assert_eq!(record["py"]["stdout"], record["rust"]["stdout"]);
}

#[test]
fn python_stdin_write_failure_is_accounted_without_changing_python_output() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("close.py");
    std::fs::write(
        &script,
        "import os, sys\nos.close(0)\nsys.stdout.write('python authoritative')\nsys.exit(7)\n",
    )
    .unwrap();
    let log = tmp.path().join("shadow.jsonl");
    // More than pipe capacity makes the early close deterministically observable.
    let output = shadow(
        "bash_guard",
        &vec![b'x'; 1024 * 1024],
        &Env::new(),
        &mut python(&script),
        pg_hook::registry()["bash_guard"],
        &log,
    )
    .unwrap();
    assert_eq!(output.status.code(), Some(7));
    assert_eq!(output.stdout, b"python authoritative");
    assert!(output.stderr.is_empty());
    let record = record(&log);
    assert!(record["same"].is_null());
    assert_eq!(record["harness"]["error"], "Python stdin write failed");
    assert_eq!(record["py"]["exit"], 7);
    assert_eq!(record["rust"]["status"], "NotImplemented");
}

#[cfg(unix)]
#[test]
fn jsonl_logs_are_private_on_creation_and_before_appending() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("noop.py");
    std::fs::write(&script, "").unwrap();
    let log = tmp.path().join("shadow.jsonl");
    for existing in [false, true] {
        if existing {
            std::fs::set_permissions(&log, std::fs::Permissions::from_mode(0o666)).unwrap();
        }
        let output = shadow(
            "bash_guard",
            b"",
            &Env::new(),
            &mut python(&script),
            &Hex,
            &log,
        )
        .unwrap();
        assert!(output.status.success());
        assert_eq!(
            std::fs::metadata(&log).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            std::fs::read_to_string(&log).unwrap().lines().count(),
            if existing { 2 } else { 1 }
        );
    }
}
