#![cfg(unix)]
//! `pg-spawnd steps` runs a JSON plan from stdin with no shell and exits with its code.
use std::{
    io::Write,
    process::{Command, Stdio},
};

fn steps(plan: &str) -> (Option<i32>, String) {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
    cmd.arg("steps");
    steps_using(cmd, plan)
}

fn steps_using(mut cmd: Command, plan: &str) -> (Option<i32>, String) {
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(plan.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    (out.status.code(), String::from_utf8(out.stdout).unwrap())
}

#[test]
fn the_plan_on_stdin_runs_and_the_receipt_surrounds_the_spawn_output() {
    let (code, out) =
        steps(r#"{"steps":[{"argv":["echo","{}"],"receipt":{"begin":"B","end":"E"}}]}"#);
    assert_eq!((code, out.as_str()), (Some(0), "B\n{}\n\nE\n"));
}

#[test]
fn the_exit_code_is_the_first_failing_steps_and_a_bad_plan_is_a_usage_error() {
    assert_eq!(steps(r#"{"steps":[{"argv":["false"]}]}"#).0, Some(1));
    assert_eq!(steps("{").0, Some(2));
}

#[test]
fn readiness_followup_steps_supply_guest_defaults_without_replacing_process_values() {
    let guest = tempfile::tempdir().unwrap();
    let home = guest.path();
    let config = home.join("config");
    std::fs::create_dir_all(config.join("plangraph")).unwrap();
    std::fs::write(
        config.join("plangraph/plangraph.env"),
        "PG_URL=https://file.example.invalid\nPG_TOKEN_FILE='~/keys/keeper.token'\n\
         PLANGRAPH_HOST_NAME=wsl-worker\nCLAUDE_CODE_SHELL=/bin/bash\nPG_TOKEN=fixture-inline-secret\nPATH=/unusable\n",
    )
    .unwrap();
    std::os::unix::fs::symlink(env!("CARGO_BIN_EXE_pg-spawnd"), home.join("pg-spawnd")).unwrap();
    let mut cmd = Command::new("/usr/bin/env");
    cmd.env_clear()
        .env("HOME", home)
        .env("XDG_CONFIG_HOME", config)
        .env("PATH", "/usr/bin:/bin")
        .env("PG_URL", "https://override.example.invalid")
        .args([
            format!("PATH={}:/usr/bin:/bin", home.display()),
            "pg-spawnd".into(),
            "steps".into(),
        ]);
    let (code, out) = steps_using(cmd, &serde_json::json!({"host_env_defaults": true, "steps": [{
        "argv": ["/bin/sh", "-c", "printf '%s\\n' \"$PG_URL\" \"$PG_TOKEN_FILE\" \"$PLANGRAPH_HOST_NAME\" \"$CLAUDE_CODE_SHELL\" \"${PG_TOKEN-unset}\"; command -v pg-spawnd"]
    }]}).to_string());
    assert_eq!(code, Some(0));
    assert_eq!(out, format!("https://override.example.invalid\n{0}/keys/keeper.token\nwsl-worker\n/bin/bash\nunset\n{0}/pg-spawnd\n", home.display()));
}
