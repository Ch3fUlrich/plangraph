use super::sandbox::{install_pinned_file, Launcher, Sandbox, PROMPT, SID};
use serde_json::{json, Value};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

const OLD: &str = "11111111-1111-1111-1111-111111111111";

// The pinned watchdog still imports the Python launcher for identity checks.
// Redirect its subprocess calls to the candidate while keeping that import intact.
const RECYCLE: &str = r#"
import json, pathlib, runpy, subprocess, sys
watchdog, candidate = sys.argv[1:]
wd = runpy.run_path(watchdog)
old = json.loads(pathlib.Path("agents.json").read_text())[0]
launch = str(pathlib.Path(watchdog).with_name("fleet-launch"))
run = subprocess.run
def invoke(argv, *args, **kwargs):
    if candidate and argv[0] == launch:
        argv = [candidate, "session", *argv[1:]]
    return run(argv, *args, **kwargs)
subprocess.run = invoke
wd["recycle"](old, pathlib.Path("handoff.md").resolve(), "interruption fixture")
"#;

pub fn check(checkout: &Path) {
    for rust in [false, true] {
        for adopt in [false, true] {
            let s = Sandbox::new(&Launcher::Pinned(checkout.to_owned()));
            for file in [
                "bin/ctx-watchdog",
                "bin/role_relaunch.py",
                "bin/routerctl",
                "bin/ctx-watchdog-watch",
                "bin/resume-digest-carried",
            ] {
                install_pinned_file(checkout, &s.path("router"), file);
            }
            fs::write(
                s.path("router/projects.yaml"),
                "projects:\n  - id: fixture\n    path: work\n    worktrees: worktrees\n",
            )
            .unwrap();
            fs::write(s.path("handoff.md"), "# Fixture handoff\n").unwrap();
            fs::write(
                s.path("handoff.card.md"),
                "# review card · review-0001 · 2026-11-22T05:00Z\n",
            )
            .unwrap();
            let previous = json!({
                "name": "review", "cwd": s.path("work"), "model": "sonnet", "effort": "high",
                "prompt_file": s.path("prompt"), "card": s.path("handoff.card.md"), "mcp": s.path("mcp.json"), "session_id": OLD,
                "session_name": "review-0001", "job_id": "old", "seq": 1, "ts": 1
            });
            s.write_json("state/launch/review.json", previous.clone());
            let old = json!({"name":"review-0001", "kind":"background", "status":"idle",
                "cwd":s.path("work"), "id":"old", "sessionId":OLD, "startedAt":1});
            s.write_json("agents.json", json!([old]));
            fs::write(s.path("state/session-seq"), "1").unwrap();
            fs::write(s.path("child.stdout"), "backgrounded no-id\n").unwrap();

            let output = recycle(&s, rust);
            s.expect(&output, 0, "", "");
            assert!(!s.path("stopped").exists());
            assert_eq!(s.json("state/launch/review.json"), previous);
            let pending_path = "state/launch/review.pending.json";
            let mut pending = s.json(pending_path);
            assert_eq!(pending["session_id"], Value::Null);
            assert_eq!(pending["session_name"], "review-0002");
            assert!(pending["absent_at"].is_number());
            let recycle_path = format!("state/recycle/{OLD}.json");
            let mut record = s.json(&recycle_path);
            assert_eq!(record["status"], "launching");
            assert!(fs::read_to_string(s.path("watchdog-inbox"))
                .unwrap()
                .contains("launch output has no id"));
            let child = s.json("child.json");
            let prompt = child["argv"]
                .as_array()
                .unwrap()
                .last()
                .unwrap()
                .as_str()
                .unwrap();
            let handoff = s.path("handoff.md");
            assert!(prompt.starts_with(&format!("Restart from handoff {}.", handoff.display())));
            assert!(prompt.ends_with(PROMPT), "{prompt}");
            assert_eq!(
                prompt.matches(handoff.to_str().unwrap()).count(),
                1,
                "{prompt}"
            );
            fs::remove_file(s.path("child.json")).unwrap();

            if adopt {
                let newer = json!({"name":"review-0002", "kind":"background", "status":"idle",
                    "cwd":s.path("work"), "id":"new", "sessionId":SID, "startedAt":2});
                s.write_json("agents.json", json!([old, newer]));
            } else {
                record["attempt_ts"] = json!(1);
                pending["absent_at"] = json!(1);
                s.write_json(&recycle_path, record);
                s.write_json(pending_path, pending);
            }
            let output = recycle(&s, rust);
            s.expect(&output, 0, "", "");
            assert_eq!(
                fs::read_to_string(s.path("state/session-seq")).unwrap(),
                "2"
            );
            assert!(!s.path("child.json").exists());
            assert!(!s.path(pending_path).exists());
            let record = s.json(&recycle_path);
            if adopt {
                assert_eq!(fs::read_to_string(s.path("stopped")).unwrap(), "old");
                assert_eq!(record["status"], "confirmed");
                assert_eq!(record["replacement"], SID);
                assert_eq!(record["stop_status"], "stopped");
                let role = s.json("state/launch/review.json");
                assert_eq!(role["session_id"], SID);
                assert_eq!(role["seq"], 2);
            } else {
                assert!(!s.path("stopped").exists());
                assert_eq!(record["status"], "failed");
                assert_eq!(s.json("state/launch/review.json"), previous);
                assert!(fs::read_to_string(s.path("watchdog-inbox"))
                    .unwrap()
                    .contains(
                    "launch outcome unknown after 10 minutes; no live replacement; retry next beat"
                ));
            }
        }
    }
}

fn recycle(s: &Sandbox, rust: bool) -> Output {
    let mut cmd = Command::new("/usr/bin/timeout");
    s.isolate(&mut cmd);
    let candidate = if rust {
        env!("CARGO_BIN_EXE_pg-spawnd")
    } else {
        ""
    };
    cmd.args(["--kill-after=2s", "30s", "/usr/bin/python3", "-c", RECYCLE])
        .arg(s.path("router/bin/ctx-watchdog"))
        .arg(candidate)
        .env("AUTOOS_CTX_STATE", s.path("guard"))
        .env("CTX_WATCHDOG_INBOX", s.path("watchdog-inbox"))
        .env("CTX_WATCHDOG_CONFIRM_S", "0")
        .output()
        .unwrap()
}

// Golden launcher boundary from the pinned watchdog's first recycle attempt.
// The full pinned watchdog test below this boundary also checks its later beats.
pub fn check_launcher_contract(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    let handoff = s.path("handoff.md");
    let card = handoff.with_extension("card.md");
    fs::write(&handoff, "# Fixture handoff\n").unwrap();
    fs::write(&card, "# review card · review-0001 · 2026-11-22T05:00Z\n").unwrap();
    let previous = json!({
        "name": "review", "cwd": s.path("work"), "model": "sonnet", "effort": "high",
        "prompt_file": s.path("prompt"), "card": card, "handoff": handoff,
        "mcp": s.path("mcp.json"), "session_id": OLD, "session_name": "review-0001",
        "job_id": "old", "seq": 1, "ts": 1,
    });
    s.write_json("state/launch/review.json", previous.clone());
    fs::write(s.path("state/session-seq"), "1").unwrap();
    let restart = s.path("restart");
    fs::write(
        &restart,
        format!(
            "Restart from handoff {}. Run resume-digest first.\n\n{PROMPT}",
            handoff.display()
        ),
    )
    .unwrap();
    fs::write(s.path("child.stdout"), "backgrounded no-id\n").unwrap();

    let output = s
        .command()
        .arg("review")
        .arg(s.path("work"))
        .args(["sonnet", "high"])
        .arg(restart)
        .args(["--respawn", "--mcp"])
        .arg(s.path("mcp.json"))
        .env("FLEET_DEFER_RECORD", "1")
        .output()
        .unwrap();
    assert!(output.status.success(), "{}", s.normalize(&output.stderr));
    assert_eq!(
        s.normalize(&output.stdout),
        concat!(
            "backgrounded no-id\nfleet-launch: session review-0002\n",
            "fleet-launch: started-unknown review-0002\n",
        )
    );
    assert_eq!(s.json("state/launch/review.json"), previous);
    let pending = s.json("state/launch/review.pending.json");
    assert_eq!(pending["session_name"], "review-0002");
    assert!(pending["session_id"].is_null());
    assert_eq!(pending, s.json("state/launch/sessions/review-0002.json"));
    let child = s.json("child.json");
    let prompt = child["argv"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .as_str()
        .unwrap();
    assert!(prompt.starts_with(&format!("Restart from handoff {}.", handoff.display())));
    assert!(prompt.ends_with(PROMPT), "{prompt}");
}
