use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Output, Stdio};

fn run(transcript: &[Value], ledger: &str) -> Output {
    let dir = tempfile::tempdir().unwrap();
    let transcript_path = dir.path().join("transcript.jsonl");
    let ledger_path = dir.path().join("lesson-session.tsv");
    std::fs::write(
        &transcript_path,
        transcript
            .iter()
            .map(|row| format!("{row}\n"))
            .collect::<String>(),
    )
    .unwrap();
    std::fs::write(&ledger_path, ledger).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .arg("lesson-stop")
        .env("PG_LESSON_LEDGER_DIR", dir.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(
        child.stdin.take().unwrap(),
        "{}",
        json!({
            "hook_event_name": "Stop", "session_id": "session", "transcript_path": transcript_path,
        })
    )
    .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn stop_requires_a_ledger_entry_for_each_signal() {
    let transcript = [
        json!({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool1", "name": "Bash"},
            {"type": "tool_use", "id": "tool2", "name": "mcp__keeper__define"}
        ]}}),
        json!({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool1", "is_error": true},
            {"type": "tool_result", "tool_use_id": "tool2", "content": "Refused: claim held"}
        ]}}),
        json!({"type": "user", "message": {"content": "<teammate-message teammate_id=\"peer\">BLOCKED: no response</teammate-message>"}}),
        json!({"type": "user", "message": {"content": "Correction: use the logged time."}}),
        json!({"type": "assistant", "message": {"content": "FAIL: this is the assistant's report"}}),
    ];
    let partial =
        "tool:tool1\tplangraph:LE-COMMAND\ntool:tool2\tattached to plangraph:KEEPER-HYGIENE\n";
    let result = run(&transcript, partial);
    assert_eq!(result.status.code(), Some(2));
    let stderr = String::from_utf8(result.stderr).unwrap();
    assert!(stderr.contains("message:3:1 peer BLOCKED"), "{stderr}");
    assert!(
        stderr.contains("message:4:1 operator correction"),
        "{stderr}"
    );
    assert!(!stderr.contains("tool:tool1"), "{stderr}");
    assert!(!stderr.contains("tool:tool2"), "{stderr}");
    let complete = format!("{partial}message:3:1\tLE-PEER\nmessage:4:1\tLE-TIME\n");
    assert!(run(&transcript, &complete).status.success());
}

#[test]
fn codex_tool_exits_are_checked_on_stop() {
    let transcript = [
        json!({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "call_one"}}),
        json!({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_one", "output": "Chunk ID: abc\nProcess exited with code 1\nOutput:\nfixture"}}),
    ];
    let result = run(&transcript, "tool:call_one\tunrelated keeper item\n");
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8(result.stderr)
        .unwrap()
        .contains("tool:call_one"));
}

#[cfg(unix)]
#[test]
fn session_installer_builds_copies_and_checksums_the_hook() {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;

    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let fixture = tempfile::tempdir().unwrap();
    let dest = fixture.path().join("installed");
    let bin = fixture.path().join("bin");
    let target = fixture.path().join("mock-build");
    fs::create_dir(&bin).unwrap();
    let cargo = bin.join("cargo");
    pg_worker_core::executable_test_support::write_executable(
        &cargo,
        concat!(
            "#!/bin/sh\nset -eu\n",
            "printf '%s\\n' \"$@\" > \"$CARGO_TARGET_DIR/argv\"\n",
            "mkdir \"$CARGO_TARGET_DIR/debug\"\n",
            "printf 'fixture binary' > \"$CARGO_TARGET_DIR/debug/pg-hook\"\n"
        ),
    );
    let path = std::env::join_paths(
        std::iter::once(bin).chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
    )
    .unwrap();
    let mut install = Command::new("sh");
    install
        .arg(root.join("tools/hooks/install_hooks.sh"))
        .arg("--session-rust")
        .env("DEST", &dest)
        .env("PATH", path);

    let refused = install.env_remove("CARGO_TARGET_DIR").output().unwrap();
    assert!(!refused.status.success(), "{refused:?}");
    assert!(String::from_utf8_lossy(&refused.stderr)
        .contains("set the shared host CARGO_TARGET_DIR before installing Rust hooks"));
    assert!(!dest.exists());

    fs::create_dir(&target).unwrap();
    let installed = install.env("CARGO_TARGET_DIR", &target).output().unwrap();
    assert!(installed.status.success(), "{installed:?}");
    let argv = fs::read_to_string(target.join("argv")).unwrap();
    let manifest = root.canonicalize().unwrap().join("rust/Cargo.toml");
    assert_eq!(
        argv.lines().collect::<Vec<_>>(),
        [
            "build",
            "--manifest-path",
            manifest.to_str().unwrap(),
            "-p",
            "pg-hook",
            "--bin",
            "pg-hook"
        ]
    );
    let hook = dest.join("pg-hook");
    assert_eq!(fs::read(&hook).unwrap(), b"fixture binary");
    assert_ne!(fs::metadata(&hook).unwrap().permissions().mode() & 0o111, 0);
    let check = Command::new("sha256sum")
        .args(["-c", "SHA256SUMS"])
        .current_dir(dest)
        .output()
        .unwrap();
    assert!(check.status.success(), "{check:?}");
    assert!(String::from_utf8_lossy(&check.stdout)
        .lines()
        .any(|line| line == "pg-hook: OK"));
}
