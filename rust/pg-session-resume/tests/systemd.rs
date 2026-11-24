//! A resumed writer must survive the completed timer tick. Runs the real binary as a
//! transient systemd user unit with the deployed unit's KillMode; skipped (with a note)
//! where `systemd-run --user` is not available.

use pg_session_resume::system::proc_start_ticks;
use serde_json::{json, Value};
use std::path::Path;
use std::process::Command;

const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";

fn systemd_user_available() -> bool {
    Command::new("systemd-run")
        .args([
            "--user",
            "--wait",
            "--collect",
            "-p",
            "Type=oneshot",
            "true",
        ])
        .output()
        .is_ok_and(|o| o.status.success())
}

fn deployed_kill_mode() -> String {
    let unit = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../deploy/systemd/pg-session-resume.service");
    let text = std::fs::read_to_string(unit).unwrap();
    text.lines()
        .find(|l| l.starts_with("KillMode="))
        .expect("KillMode in the unit")
        .to_owned()
}

#[test]
fn a_resumed_writer_survives_the_tick_unit_and_receives_the_findings() {
    if !systemd_user_available() {
        eprintln!("skipped: no systemd user manager");
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let (state, pr, bin, wt) = (
        dir.path().join("state"),
        dir.path().join("pr"),
        dir.path().join("bin"),
        dir.path().join("lane-a"),
    );
    for d in [&state.join("launch/sessions"), &pr, &bin, &wt] {
        std::fs::create_dir_all(d).unwrap();
    }
    // a stand-in `codex`: records its arguments, then stays alive like a writer in a fix round
    let fake = bin.join("codex");
    std::fs::write(
        &fake,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PG_TEST_OUT\"\nexec sleep 20\n",
    )
    .unwrap();
    std::fs::set_permissions(&fake, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let rec = json!({"name": "writer", "session_name": "writer-1", "cwd": wt, "agent": "codex", "thread_id": "T-1", "ts": 1,
        "state": "stopped", "stopped_ts": 2,
        "resume": {"agent": "codex", "handle": "T-1", "head": null, "status_head": HEAD, "round": 0, "stopped_ts": 2}});
    std::fs::write(state.join("launch/sessions/writer-1.json"), rec.to_string()).unwrap();
    std::fs::write(pr.join("lane-a.fix-round-1.head"), HEAD).unwrap();
    std::fs::write(pr.join("lane-a.fix-round-1.txt"), "fix the thing\n").unwrap();
    let out = dir.path().join("args");

    let run = Command::new("systemd-run")
        .args([
            "--user",
            "--wait",
            "--collect",
            "-p",
            "Type=oneshot",
            "-p",
            &deployed_kill_mode(),
        ])
        .arg(format!("--setenv=PATH={}:/usr/bin:/bin", bin.display()))
        .arg(format!("--setenv=PG_TEST_OUT={}", out.display()))
        .arg(format!("--setenv=HOME={}", dir.path().display()))
        .arg(env!("CARGO_BIN_EXE_pg-session-resume"))
        .args(["resume", "--min-free-mb", "0", "--state"])
        .arg(&state)
        .arg("--pr-dir")
        .arg(&pr)
        .output()
        .unwrap();
    assert!(
        run.status.success(),
        "{}",
        String::from_utf8_lossy(&run.stderr)
    );

    // the unit is gone; the writer is not, and it got exactly the findings text
    let rec: Value = serde_json::from_slice(
        &std::fs::read(state.join("launch/sessions/writer-1.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        rec["resume"]["round"], 1,
        "acknowledged by the live process"
    );
    let (pid, start) = (
        rec["pid"].as_u64().expect("pid recorded"),
        rec["pid_start"].as_u64().unwrap(),
    );
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .expect("the writer outlived the tick unit");
    assert_eq!(proc_start_ticks(&stat), Some(start));
    for _ in 0..50 {
        if out.exists() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    let args = std::fs::read_to_string(&out).unwrap();
    assert_eq!(args, "exec\nresume\nT-1\nfix the thing\n\n");
    let _ = Command::new("kill").arg(pid.to_string()).status();
}
