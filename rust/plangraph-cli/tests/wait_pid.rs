use std::fs;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn cli(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(args)
        .output()
        .unwrap()
}

#[cfg(windows)]
#[test]
fn windows_fixture() {
    if std::env::var_os("PG_WAIT_PID_FIXTURE").is_none() {
        return;
    }
    let mut buf = [0u8; 1];
    let _ = std::io::Read::read(&mut std::io::stdin(), &mut buf);
}

#[cfg(unix)]
#[test]
fn loop_waiting_on_pattern_in_own_argv_exits_when_target_ends() {
    let dir = tempfile::tempdir().unwrap();
    let pid_file = dir.path().join("target.pid");
    let pattern = "codex exec -m gpt-test-model";
    let plangraph = env!("CARGO_BIN_EXE_plangraph");

    let gate_file = dir.path().join("gate");
    // The outer shell's own argv literally contains `codex exec -m gpt-test-model`,
    // which caused `while pgrep -f` to self-match and never terminate.
    let script = r#"
        set -eu
        plangraph="$1"
        pidfile="$2"
        pattern="$3"
        gate="$4"

        : > "$gate"
        sh -c 'while [ -f "$1" ]; do sleep 0.02; done' sh "$gate" "$pattern" &
        target_pid=$!
        printf '%s\n' "$target_pid" > "$pidfile"
        "$plangraph" wait-pid --pid "$target_pid" --expect "$pattern" --check
        (sleep 0.05; rm -f "$gate") &
        while "$plangraph" wait-pid --pid "$target_pid" --expect "$pattern" --check; do
            sleep 0.02
        done
        wait "$target_pid" 2>/dev/null || true

        : > "$gate"
        sh -c 'while [ -f "$1" ]; do sleep 0.02; done' sh "$gate" "$pattern" &
        target_pid2=$!
        printf '%s\n' "$target_pid2" > "$pidfile"
        (sleep 0.05; rm -f "$gate") &
        "$plangraph" wait-pid --pid-file "$pidfile" --expect "$pattern" --timeout 10 --poll-ms 10
        wait "$target_pid2" 2>/dev/null || true
    "#;
    let started = Instant::now();
    let out = Command::new("sh")
        .args([
            "-c",
            script,
            "sh",
            plangraph,
            pid_file.to_str().unwrap(),
            pattern,
            gate_file.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "wait loop failed: stdout={} stderr={}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        started.elapsed() < Duration::from_secs(4),
        "wait loop took too long: {:?}",
        started.elapsed()
    );
}

#[cfg(unix)]
#[test]
fn pid_file_check_and_wait_reflect_process_lifetime() {
    let dir = tempfile::tempdir().unwrap();
    let pid_file = dir.path().join("worker.pid");

    // Missing pid file: --check exits 1, wait exits 0 immediately.
    assert_eq!(
        cli(&[
            "wait-pid",
            "--pid-file",
            pid_file.to_str().unwrap(),
            "--check"
        ])
        .status
        .code(),
        Some(1)
    );
    assert_eq!(
        cli(&[
            "wait-pid",
            "--pid-file",
            pid_file.to_str().unwrap(),
            "--timeout",
            "1",
        ])
        .status
        .code(),
        Some(0)
    );

    let mut child = Command::new("sh")
        .args(["-c", "sleep 30"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    fs::write(&pid_file, format!("{}\n", child.id())).unwrap();

    assert_eq!(
        cli(&[
            "wait-pid",
            "--pid-file",
            pid_file.to_str().unwrap(),
            "--check"
        ])
        .status
        .code(),
        Some(0)
    );
    assert_eq!(
        cli(&[
            "wait-pid",
            "--pid-file",
            pid_file.to_str().unwrap(),
            "--timeout",
            "0.05",
            "--poll-ms",
            "10",
        ])
        .status
        .code(),
        Some(1)
    );

    let _ = child.kill();
    let _ = child.wait();

    assert_eq!(
        cli(&[
            "wait-pid",
            "--pid-file",
            pid_file.to_str().unwrap(),
            "--timeout",
            "2",
            "--poll-ms",
            "10",
        ])
        .status
        .code(),
        Some(0)
    );
    assert_eq!(
        cli(&[
            "wait-pid",
            "--pid-file",
            pid_file.to_str().unwrap(),
            "--check"
        ])
        .status
        .code(),
        Some(1)
    );
}

#[cfg(unix)]
#[test]
fn expect_mismatch_does_not_treat_unrelated_pid_as_alive() {
    let mut child = Command::new("sh")
        .args(["-c", "sleep 30"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let pid_str = child.id().to_string();
    let check = cli(&[
        "wait-pid",
        "--pid",
        &pid_str,
        "--expect",
        "unrelated-target-marker",
        "--check",
    ]);
    let wait = cli(&[
        "wait-pid",
        "--pid",
        &pid_str,
        "--expect",
        "unrelated-target-marker",
        "--timeout",
        "1",
    ]);
    let _ = child.kill();
    let _ = child.wait();
    assert_eq!(check.status.code(), Some(1));
    assert_eq!(wait.status.code(), Some(0));
}

#[test]
fn invalid_arguments_and_help_return_expected_codes() {
    let help = cli(&["wait-pid", "--help"]);
    assert_eq!(help.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&help.stdout).contains("wait-pid"));

    for bad in [
        &["wait-pid"][..],
        &["wait-pid", "--pid", "0"][..],
        &["wait-pid", "--pid", "1", "--pid-file", "x.pid"][..],
        &["wait-pid", "--pid", "1", "--check", "--timeout", "1"][..],
        &["wait-pid", "--pid", "1", "--timeout", "1e300"][..],
        &["wait-pid", "--pid", "1", "--timeout", "inf"][..],
        &["wait-pid", "--pid", "1", "--timeout", "604801"][..],
    ] {
        let out = cli(bad);
        assert_eq!(out.status.code(), Some(2), "{bad:?}");
        assert!(!out.stderr.is_empty(), "{bad:?}");
    }
}

#[test]
fn corrupt_pid_file_exits_2_with_message() {
    let dir = tempfile::tempdir().unwrap();
    let pid_file = dir.path().join("corrupt.pid");
    for bad in ["not-a-pid\n", "0\n", ""] {
        fs::write(&pid_file, bad).unwrap();
        for extra in [&["--check"][..], &["--timeout", "1"][..]] {
            let mut args = vec!["wait-pid", "--pid-file", pid_file.to_str().unwrap()];
            args.extend_from_slice(extra);
            let out = cli(&args);
            assert_eq!(out.status.code(), Some(2), "{bad:?} {extra:?}");
            let stderr = String::from_utf8_lossy(&out.stderr);
            assert!(
                stderr.contains("invalid pid file"),
                "expected invalid pid file message, got: {stderr}"
            );
        }
    }
}

#[cfg(windows)]
#[test]
fn windows_expect_matches_process_image_name_and_rejects_mismatch() {
    use std::io::Write;
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "windows_fixture", "--nocapture"])
        .env("PG_WAIT_PID_FIXTURE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let pid_str = child.id().to_string();
    let matched = cli(&[
        "wait-pid", "--pid", &pid_str, "--expect", "wait_pid", "--check",
    ]);
    let mismatched = cli(&[
        "wait-pid",
        "--pid",
        &pid_str,
        "--expect",
        "unrelated-target-marker",
        "--check",
    ]);
    let wait_mismatch = cli(&[
        "wait-pid",
        "--pid",
        &pid_str,
        "--expect",
        "unrelated-target-marker",
        "--timeout",
        "1",
    ]);
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(b"x");
    }
    let _ = child.wait();
    assert_eq!(matched.status.code(), Some(0));
    assert_eq!(mismatched.status.code(), Some(1));
    assert_eq!(wait_mismatch.status.code(), Some(0));
}
