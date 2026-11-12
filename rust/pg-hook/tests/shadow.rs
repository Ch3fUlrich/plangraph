use pg_hook::{observe, registry, shadow, Env, Hook, HookError, HookOutput};
use std::io::Write;
use std::process::{Command, Stdio};

struct Fake(&'static str);
impl Hook for Fake {
    fn run(&self, event_json: &str, env: &Env) -> Result<HookOutput, HookError> {
        assert_eq!(event_json, "{not json\n");
        assert!(env.contains_key(std::ffi::OsStr::new("HOME")));
        match self.0 {
            "error" => Err(HookError::Failed("test error".into())),
            "panic" => panic!("test panic"),
            _ => Ok(HookOutput {
                exit_code: if self.0 == "same" { 7 } else { 0 },
                stdout: event_json.into(),
                stderr: String::new(),
            }),
        }
    }
}
fn python(script: &std::path::Path) -> Command {
    let mut command = Command::new("python3");
    command.arg(script);
    command
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
        assert_eq!(record["same"], mode == "same");
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
            observe(*hook, "", &Env::new()),
            serde_json::json!({"status": "NotImplemented"})
        );
    }
}

#[test]
fn shadow_cli_returns_python_and_appends_not_implemented() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("hook.py");
    let log = tmp.path().join("shadow.jsonl");
    std::fs::write(&script, "import sys\nsys.stdout.write(sys.stdin.read())\nsys.stderr.write('python diagnostic')\nsys.exit(7)\n").unwrap();
    for _ in 0..2 {
        let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
            .args(["shadow", "bash_guard"])
            .arg(&log)
            .arg(&script)
            .env_clear()
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
        assert_eq!(record["same"], false);
        assert_eq!(record["py"]["stdout"], "{not json\n");
        assert_eq!(record["rust"]["status"], "NotImplemented");
    }
}
