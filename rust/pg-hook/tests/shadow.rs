use pg_hook::{observe, registry, shadow, Env, Hook, HookError, HookOutput};
use std::io::Write;
use std::process::{Command, Stdio};

struct Fake(&'static str);
impl Hook for Fake {
    fn run(&self, event_json: &[u8], env: &Env) -> Result<HookOutput, HookError> {
        assert_eq!(event_json, b"{not json\n");
        assert!(env.contains_key(std::ffi::OsStr::new("HOME")));
        match self.0 {
            "error" => Err(HookError::Failed("test error".into())),
            "panic" => panic!("test panic"),
            _ => Ok(HookOutput {
                exit_code: if self.0 == "same" { 7 } else { 0 },
                stdout: String::from_utf8(event_json.to_vec()).unwrap(),
                stderr: String::new(),
            }),
        }
    }
}
fn python(script: &std::path::Path) -> Command {
    // Resolve before shadow clears the environment, including non-system installs.
    let mut command = Command::new(python_executable());
    command.arg(script);
    command
}
fn python_executable() -> std::path::PathBuf {
    let output = Command::new("python3")
        .args(["-I", "-c", "import sys; print(sys.executable)"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let path = std::path::PathBuf::from(String::from_utf8(output.stdout).unwrap().trim());
    assert!(path.is_absolute());
    path
}

fn python_path(tmp: &std::path::Path) -> std::path::PathBuf {
    let bin = tmp.join("bin");
    std::fs::create_dir(&bin).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(python_executable(), bin.join("python3")).unwrap();
    #[cfg(not(unix))]
    std::fs::copy(python_executable(), bin.join("python3.exe")).unwrap();
    bin
}

#[test]
fn python_wins_over_rust_differences_errors_panics_and_log_failures() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    std::fs::write(
        &script,
        "import sys\nsys.stdout.write(sys.stdin.read()); sys.exit(7)\n",
    )
    .unwrap();
    let env = Env::from([("HOME".into(), tmp.path().as_os_str().to_owned())]);
    let input = b"{not json\n";
    for mode in ["different", "error", "panic", "same"] {
        let log = tmp.path().join(mode);
        let mut cmd = python(&script);
        let py = shadow("bash_guard", input, &env, &mut cmd, &Fake(mode), &log).unwrap();
        assert_eq!(py.status.code(), Some(7));
        assert_eq!(py.stdout, b"{not json\n");
        let record: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(log).unwrap()).unwrap();
        if matches!(mode, "error" | "panic") {
            assert!(record["same"].is_null(), "{record}");
        } else {
            assert_eq!(record["same"], mode == "same");
        }
        assert_eq!(record["py"]["exit"], 7);
        assert_eq!(record["py"]["stdout"], "{not json\n");
        let key = match mode {
            "error" => "error",
            "panic" => "panic",
            _ => "stdout",
        };
        assert_eq!(
            record["rust"][key],
            match mode {
                "error" => "test error",
                "panic" => "Rust hook panicked",
                _ => "{not json\n",
            }
        );
    }
    let mut cmd = python(&script);
    let bad_log = tmp.path();
    let py = shadow("bash_guard", input, &env, &mut cmd, &Fake("panic"), bad_log).unwrap();
    assert_eq!(py.status.code(), Some(7));
}

#[test]
fn registry_has_exactly_five_unimplemented_entries() {
    let hooks = registry();
    assert_eq!(
        hooks.keys().copied().collect::<Vec<_>>().join(","),
        "bash_guard,ctx_guard_session,fleet_inject_post,fleet_inject_pre,fleet_report"
    );
    for hook in hooks.values() {
        assert_eq!(
            observe(*hook, b"", &Env::new()),
            serde_json::json!({"status": "NotImplemented"})
        );
    }
}

#[test]
fn pending_hooks_have_no_parity_decision() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    std::fs::write(
        &script,
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.exit(7)\n",
    )
    .unwrap();
    let log = tmp.path().join("pending.jsonl");
    for (entry, hook) in registry() {
        let output = shadow(
            entry,
            b"{not json\n",
            &Env::new(),
            &mut python(&script),
            hook,
            &log,
        )
        .unwrap();
        assert_eq!(output.status.code(), Some(7));
        assert_eq!(output.stdout, b"{not json\n");
        assert!(output.stderr.is_empty());
    }
    let text = std::fs::read_to_string(log).unwrap();
    assert_eq!(text.lines().count(), 5);
    for line in text.lines() {
        let record: serde_json::Value = serde_json::from_str(line).unwrap();
        assert!(record["same"].is_null(), "{record}");
        assert_eq!(record["py"]["exit"], 7);
        assert_eq!(record["rust"]["status"], "NotImplemented");
        assert!(record.get("harness").is_none());
    }
}

#[test]
fn shadow_cli_returns_python_and_appends_not_implemented() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    let log = tmp.path().join("shadow.jsonl");
    let path = python_path(tmp.path());
    std::fs::write(&script, "import sys\nsys.stdout.write(sys.stdin.read())\nsys.stderr.write('python diagnostic')\nsys.exit(7)\n").unwrap();
    for _ in 0..2 {
        let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
            .args(["shadow", "bash_guard"])
            .arg(&log)
            .arg(&script)
            .env_clear()
            .env("PATH", &path)
            .env("HOME", tmp.path())
            .env("XDG_STATE_HOME", tmp.path().join("state"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let _ = child.stdin.take().unwrap().write_all(b"{not json\n");
        let output = child.wait_with_output().unwrap();
        assert_eq!(output.status.code(), Some(7));
        assert_eq!(output.stdout, b"{not json\n");
        assert_eq!(output.stderr, b"python diagnostic");
    }
    let text = std::fs::read_to_string(log).unwrap();
    assert_eq!(text.lines().count(), 2);
    for line in text.lines() {
        let record: serde_json::Value = serde_json::from_str(line).unwrap();
        assert_eq!(record["entry"], "bash_guard");
        assert!(record["same"].is_null(), "{record}");
        assert_eq!(record["py"]["stdout"], "{not json\n");
        assert_eq!(record["rust"]["status"], "NotImplemented");
    }
}

#[cfg(unix)]
#[test]
fn signal_exit_codes_match_the_logged_python_status() {
    struct Signaled;
    impl Hook for Signaled {
        fn run(&self, _: &[u8], _: &Env) -> Result<HookOutput, HookError> {
            Ok(HookOutput {
                exit_code: 143,
                stdout: String::new(),
                stderr: String::new(),
            })
        }
    }
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("killed.py");
    std::fs::write(
        &script,
        "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
    )
    .unwrap();
    let log = tmp.path().join("signal.jsonl");
    let output = shadow(
        "bash_guard",
        b"",
        &Env::new(),
        &mut python(&script),
        &Signaled,
        &log,
    )
    .unwrap();
    assert_eq!(output.status.code(), None);
    assert_eq!(pg_hook::exit_code(&output), 143);
    let record: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(log).unwrap()).unwrap();
    assert_eq!(record["py"]["exit"], 143);
    assert_eq!(record["rust"]["exit"], 143);
    assert_eq!(record["same"], true);

    let output = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .args(["shadow", "bash_guard"])
        .arg(tmp.path().join("cli.jsonl"))
        .arg(script)
        .env_clear()
        .env("PATH", python_path(tmp.path()))
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(143));
    assert!(output.stdout.is_empty());
    assert!(output.stderr.is_empty());
}

#[test]
fn shadow_cli_fails_open_for_usage_and_python_spawn_errors() {
    let tmp = tempfile::tempdir().unwrap();
    for args in [vec![], vec!["shadow", "unknown", "log", "hook.py"]] {
        let output = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
            .args(args)
            .env_clear()
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty());
        assert!(String::from_utf8(output.stderr)
            .unwrap()
            .starts_with("usage:"));
    }
    let log = tmp.path().join("shadow.jsonl");
    let output = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .args(["shadow", "bash_guard"])
        .arg(&log)
        .arg(tmp.path().join("hook.py"))
        .env_clear()
        .env("PATH", tmp.path())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, b"pg-hook: Python spawn failed\n");
    let record: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&log).unwrap()).unwrap();
    assert!(record["same"].is_null());
    assert!(record["py"].is_null());
    assert_eq!(record["rust"]["status"], "NotImplemented");
    assert_eq!(record["harness"]["error"], "Python spawn failed");

    // Diagnostics are also best effort when stderr is not writable.
    let diagnostic = tmp.path().join("diagnostic");
    std::fs::write(&diagnostic, "").unwrap();
    for args in [vec![], vec!["shadow", "bash_guard", "log", "hook.py"]] {
        let output = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
            .args(args)
            .current_dir(tmp.path())
            .env_clear()
            .env("PATH", tmp.path())
            .stderr(std::fs::File::open(&diagnostic).unwrap())
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty());
    }
    assert_eq!(std::fs::read(diagnostic).unwrap(), b"");
}

#[cfg(unix)]
#[test]
fn shadow_cli_fails_open_for_stdin_read_errors() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    std::fs::write(
        &script,
        "import sys\nopen(sys.argv[0] + '.ran', 'w').close()\n",
    )
    .unwrap();
    let log = tmp.path().join("shadow.jsonl");
    // Reading a directory fails with EISDIR, unlike an ordinary EOF on stdin.
    let output = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .args(["shadow", "bash_guard"])
        .arg(&log)
        .arg(&script)
        .env_clear()
        .env("PATH", python_path(tmp.path()))
        .stdin(std::fs::File::open(tmp.path()).unwrap())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, b"pg-hook: stdin read failed\n");
    let record: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&log).unwrap()).unwrap();
    assert!(record["same"].is_null());
    assert!(record["py"].is_null());
    assert!(record["rust"].is_null());
    assert_eq!(record["harness"]["error"], "stdin read failed");
    assert!(!script.with_extension("py.ran").exists());
}
