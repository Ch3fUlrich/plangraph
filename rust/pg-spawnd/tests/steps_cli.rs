#![cfg(unix)]
//! `pg-spawnd steps` runs a JSON plan from stdin with no shell and exits with its code.
use std::{
    io::Write,
    process::{Command, Stdio},
};

fn steps(plan: &str) -> (Option<i32>, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .arg("steps")
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
