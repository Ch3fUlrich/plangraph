use super::{
    activity::strip_ansi,
    capture::Capture,
    group::StopOutcome,
    record::{RunRecord, Status},
    supervise::FinishHook,
};
use serde_json::json;
use std::{
    panic::{catch_unwind, AssertUnwindSafe},
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn save(record: &RunRecord) -> bool {
    if record.save().is_ok() {
        true
    } else {
        eprintln!("run record save failed");
        false
    }
}
pub fn output(record: &mut RunRecord, capture: &Capture, final_output: bool) -> u64 {
    let s = capture.snapshot();
    record.set(
        "stdout_tail",
        json!(if final_output {
            s.stdout_final
        } else {
            s.stdout_recent
        }),
    );
    record.set(
        "stderr_tail",
        json!(if final_output {
            s.stderr_final
        } else {
            s.stderr_recent
        }),
    );
    record.set("last_activity_ts", json!(s.last_activity_ts));
    for e in capture.take_events() {
        let _ = record.push_event(e);
    }
    s.seq
}
pub fn finish(
    record: &mut RunRecord,
    hook: &mut Option<FinishHook>,
    code: i32,
    cancelled: bool,
    stop: StopOutcome,
    capture: Option<&Capture>,
) {
    if let Some(c) = capture {
        output(record, c, true);
    }
    let mut code = code;
    let status = match stop {
        StopOutcome::Survivors(mut pids) => {
            pids.sort_unstable();
            if code == 0 {
                code = 1;
            }
            record.set("reason", json!("kill_incomplete"));
            record.set("survivors", json!(pids));
            let _ = record.push_event(json!({"event":"kill_incomplete", "survivors":pids}));
            Status::Failed
        }
        StopOutcome::Empty if cancelled => {
            code = 1;
            record.set("reason", json!("cancelled"));
            Status::Cancelled
        }
        StopOutcome::Empty if code == 0 => Status::Completed,
        _ => {
            if record.get("reason").is_none() {
                let text = strip_ansi(
                    record
                        .get("stderr_tail")
                        .and_then(|v| v.as_str())
                        .unwrap_or(""),
                );
                let first = text
                    .lines()
                    .map(str::trim)
                    .find(|s| !s.is_empty())
                    .map(|s| s.chars().take(300).collect::<String>());
                record.set(
                    "reason",
                    json!(first
                        .clone()
                        .unwrap_or_else(|| format!("harness exit {code}"))),
                );
                record.set(
                    "error",
                    json!(first.unwrap_or_else(|| format!("harness exit code {code}"))),
                );
            }
            Status::Failed
        }
    };
    record.set("exit_code", json!(code));
    if let Some(h) = hook.take() {
        if catch_unwind(AssertUnwindSafe(|| h(record, status))).is_err() {
            let _ = record.push_event(json!({"event":"finish_hook_failed"}));
        }
    }
    let _ = record.transition(status, now());
    for attempt in 0..3 {
        if save(record) {
            break;
        }
        if attempt < 2 {
            thread::sleep(Duration::from_millis(100));
        }
    }
}
