#![cfg(target_os = "linux")]

mod spawn_attempt_delivery;
#[path = "../../test_support/python.rs"]
mod test_python;

use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

struct Fixture {
    root: tempfile::TempDir,
    checkout: PathBuf,
    target: PathBuf,
    bin: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let checkout = root.path().join("checkout");
        let target = root.path().join("cargo-target");
        let bin = root.path().join("bin");
        for dir in [&checkout, &target, &bin] {
            fs::create_dir(dir).unwrap();
        }
        fs::create_dir(checkout.join(".git")).unwrap();
        Self {
            root,
            checkout,
            target,
            bin,
        }
    }

    fn executable(&self, name: &str, body: &str) -> PathBuf {
        let path = self.bin.join(name);
        pg_worker_core::executable_test_support::write_private_executable(
            &path,
            format!("#!/bin/sh\n{body}\n"),
        );
        path
    }

    fn command(&self, target: Option<&Path>) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
        cmd.current_dir(&self.checkout)
            .env_clear()
            .env("HOME", self.root.path())
            .env("PATH", &self.bin)
            .env("PLANGRAPH_CARGO_TARGET_DIR", &self.target)
            .env("FIXTURE_MARKER", self.root.path().join("spawned"));
        if let Some(target) = target {
            cmd.env("CARGO_TARGET_DIR", target);
        }
        cmd.arg("hand-run");
        cmd
    }
}

#[test]
fn writer_refuses_missing_empty_or_checkout_local_target_before_spawn() {
    let fixture = Fixture::new();
    let marker = fixture.root.path().join("spawned");
    let writer = fixture.executable(
        "codex",
        r#"printf '%s' "$CARGO_TARGET_DIR" > "$FIXTURE_MARKER""#,
    );
    let local = fixture.checkout.join("rust/target");
    for value in [None, Some(Path::new("")), Some(local.as_path())] {
        let output = fixture
            .command(value)
            .arg("--")
            .arg(&writer)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("CARGO_TARGET_DIR"));
        assert!(!marker.exists());
    }
    let output = fixture
        .command(Some(&fixture.target))
        .arg("--")
        .arg(&writer)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        fs::read_to_string(marker).unwrap(),
        fixture.target.display().to_string()
    );
}

fn detached_fixture() -> Fixture {
    let fixture = Fixture::new();
    fixture.executable("git", "exit 0");
    fixture.executable("cargo", "exit 0");
    fixture.executable(
        "systemd-run",
        r#"
        : > "$FIXTURE_MARKER"
        export PATH=/manager/path
        while [ "$1" != -- ]; do
            [ "$#" -gt 0 ] || exit 2
            case "$1" in
                --setenv=*) export "${1#--setenv=}" ;;
            esac
            shift
        done
        shift
        exec "$@"
    "#,
    );
    fixture
}

#[test]
fn hand_writer_and_external_review_record_terminal_outcomes() {
    let fixture = detached_fixture();
    let state = fixture.root.path().join("spawner");
    let writer = fixture.executable("codex", "exit 0");
    let reviewer = fixture.executable("agy", "kill -TERM $$");
    let foreground_reviewer = fixture.executable("claude", "kill -TERM $$");
    let run = |target| {
        let mut command = fixture.command(target);
        command
            .env("PLANGRAPH_SPAWN_STATE", &state)
            .env("PLANGRAPH_HOST", "workstation");
        command
    };
    assert!(run(Some(fixture.target.as_path()))
        .arg("--")
        .arg(writer)
        .status()
        .unwrap()
        .success());
    let review = fixture.root.path().join("review");
    assert_eq!(
        run(None)
            .args([
                "--unit-child",
                fixture.root.path().join("result").to_str().unwrap(),
                review.to_str().unwrap(),
                "--"
            ])
            .arg(reviewer)
            .status()
            .unwrap()
            .code(),
        Some(143)
    );
    assert_eq!(
        run(None)
            .args(["--review", "--"])
            .arg(foreground_reviewer)
            .status()
            .unwrap()
            .code(),
        Some(143)
    );
    let mut rows: Vec<Value> = fs::read_dir(state.join("runs"))
        .unwrap()
        .map(|entry| {
            serde_json::from_slice(&fs::read(entry.unwrap().path().join("record.json")).unwrap())
                .unwrap()
        })
        .collect();
    rows.sort_by_key(|v| v["harness"].as_str().unwrap().to_owned());
    assert_eq!(rows.len(), 3);
    for (row, role, harness, status) in [
        (&rows[0], "review", "agy", "cancelled"),
        (&rows[1], "review", "claude", "cancelled"),
        (&rows[2], "implement", "codex", "completed"),
    ] {
        assert_eq!(row["role"], role);
        assert_eq!(row["host"], "workstation");
        assert_eq!(row["harness"], harness);
        assert_eq!(row["status"], status);
        assert!(row["ended"].is_number());
    }
    spawn_attempt_delivery::check(
        &state,
        &[
            ("implement", "workstation", "codex", "ok"),
            ("review", "workstation", "agy", "cancelled"),
            ("review", "workstation", "claude", "cancelled"),
        ],
    );
}

#[test]
fn missing_codex_is_refused_before_systemd_starts() {
    let fixture = detached_fixture();
    let output = fixture
        .command(Some(&fixture.target))
        .args(["--unit", "writer-fixture", "--result"])
        .arg(fixture.root.path().join("result"))
        .args(["--", "codex", "exec"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("codex"));
    assert!(!fixture.root.path().join("spawned").exists());
}

#[test]
fn lifecycle_failure_preserves_harness_exit_status() {
    let fixture = Fixture::new();
    let state = fixture.root.path().join("spawner");
    for code in [42, 0] {
        let state = state.join(code.to_string());
        let reviewer = fixture.executable(
            "claude",
            &format!(
                "printf corrupt > \"$PLANGRAPH_SPAWN_STATE/run-lifecycle/published\"\nexit {code}"
            ),
        );
        let output = fixture
            .command(None)
            .env("PLANGRAPH_SPAWN_STATE", &state)
            .args(["--review", "--"])
            .arg(reviewer)
            .output()
            .unwrap();
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("invalid lifecycle publication watermark"),
            "{stderr}"
        );
        assert_eq!(output.status.code(), Some(code), "{stderr}");
    }
}

#[test]
fn watchdog_skips_external_registrations() {
    let state = tempfile::tempdir().unwrap();
    let output = test_python::command()
        .args([
            "-c",
            r#"
import sys
from types import SimpleNamespace
from plangraph.spawner.stall_probe import probe_run

state = sys.argv[1]
run = dict(run_id='run-fixture', status='running', started=1)
assert probe_run(state, run, 2000)['rule'] == 'unknown'
hung = SimpleNamespace(enabled=True, probe=lambda *args, **kwargs: {'rule': 'unknown'})
for kind in ('fleet-session', 'external-harness'):
    for mode in (None, hung):
        assert probe_run(state, dict(run, run_kind=kind), 2000, hung=mode) is None, kind
"#,
        ])
        .arg(state.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn detached_child_preserves_exit_127_and_reports_empty_review() {
    let fixture = detached_fixture();
    let result = fixture.root.path().join("result");
    fixture.executable("codex", "git && cargo && exit 127");
    let output = fixture
        .command(Some(&fixture.target))
        .args(["--unit", "writer-fixture", "--result"])
        .arg(&result)
        .args(["--", "codex", "exec"])
        .output()
        .unwrap();
    let row = fs::read_to_string(&result).unwrap();
    assert!(row.lines().last().unwrap().contains("rc=127"), "{row}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("ALERT stuck-work"));

    fixture.executable("codex", "exit 0");
    let review = fixture.root.path().join("review");
    let result = fixture.root.path().join("review-result");
    fs::write(&review, "").unwrap();
    let output = fixture
        .command(None)
        .args(["--unit", "review-fixture", "--result"])
        .arg(&result)
        .arg("--review-output")
        .arg(&review)
        .args(["--", "codex", "exec"])
        .output()
        .unwrap();
    let row = fs::read_to_string(&result).unwrap();
    assert!(
        row.lines()
            .last()
            .unwrap()
            .contains("rc=0 verdict=no_verdict"),
        "{row}"
    );
    assert!(String::from_utf8_lossy(&output.stderr).contains("ALERT stuck-work"));
}
