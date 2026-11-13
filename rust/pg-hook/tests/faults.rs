#![cfg(unix)]

mod common;

use common::python;
use pg_hook::{shadow, Env, Hook, HookError, HookOutput};
use serde_json::Value;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

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
fn shadow_cli_preserves_python_imports_and_authoritative_output() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    let modules = tmp.path().join("modules");
    std::fs::create_dir(&modules).unwrap();
    std::fs::write(tmp.path().join("sibling.py"), "message = b'sibling:'\n").unwrap();
    std::fs::write(modules.join("configured.py"), "message = b'configured:'\n").unwrap();
    std::fs::write(
        &script,
        "import sys, sibling, configured\nassert (sys.flags.isolated, sys.flags.ignore_environment, sys.flags.no_user_site) == (0, 0, 0)\nsys.stdout.buffer.write(sibling.message + configured.message + sys.stdin.buffer.read())\nsys.stderr.write('python diagnostic')\nsys.exit(7)\n",
    )
    .unwrap();
    let executable = common::python_executable();
    let bin = tmp.path().join("bin");
    std::fs::create_dir(&bin).unwrap();
    std::os::unix::fs::symlink(&executable, bin.join("python3")).unwrap();
    for (entry, override_python) in pg_hook::registry()
        .keys()
        .flat_map(|entry| [(entry.to_string(), false), (entry.to_string(), true)])
    {
        let log = tmp.path().join(format!("{entry}-{override_python}.jsonl"));
        let mut command = Command::new(env!("CARGO_BIN_EXE_pg-hook"));
        command
            .args(["shadow", &entry])
            .arg(&log)
            .arg(&script)
            .env_clear()
            .env("HOME", tmp.path())
            .env("PATH", &bin)
            .env("PYTHONPATH", &modules)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if override_python {
            command.env("PG_HOOK_PYTHON", &executable);
        }
        let mut child = command.spawn().unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(b"{not json\n")
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert_eq!(output.status.code(), Some(7), "{entry}");
        assert_eq!(output.stdout, b"sibling:configured:{not json\n");
        assert_eq!(output.stderr, b"python diagnostic");
        let record = record(&log);
        assert_eq!(record["py"]["exit"], 7);
        assert_eq!(record["py"]["stdout"], "sibling:configured:{not json\n");
        assert!(record["same"].is_null());
        assert!(record.get("harness").is_none());
    }
}

#[test]
fn relative_python_override_is_refused_with_a_named_fail_open_fault() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    std::fs::write(&script, "raise RuntimeError('must not run')\n").unwrap();
    for (index, executable) in ["python3", "./python3", ""].iter().enumerate() {
        let log = tmp.path().join(format!("relative-{index}.jsonl"));
        let output = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
            .args(["shadow", "bash_guard"])
            .arg(&log)
            .arg(&script)
            .env_clear()
            .env("HOME", tmp.path())
            .env("PG_HOOK_PYTHON", executable)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            b"pg-hook: PG_HOOK_PYTHON must be an absolute path\n"
        );
        let record = record(&log);
        assert!(record["same"].is_null());
        assert!(record["py"].is_null());
        assert!(record["rust"].is_null());
        assert_eq!(
            record["harness"]["error"],
            "PG_HOOK_PYTHON must be an absolute path"
        );
    }
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
