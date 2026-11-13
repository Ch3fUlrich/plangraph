//! Detect accepted runs that never reached a harness process.
use serde_json::{json, Value};
use std::path::Path;

fn expire(record: &mut Value, now: f64, bound: f64) -> Option<Value> {
    let started = record["started"].as_f64()?;
    if !now.is_finite()
        || !bound.is_finite()
        || bound <= 0.0
        || now < started + bound
        || !matches!(
            record["status"].as_str(),
            Some("running" | "queued" | "queue_claimed")
        )
        || record["pid"].as_u64().is_some_and(|p| p > 0)
        || record["launched_leg"].is_object()
        || record["attempts"].as_array().is_some_and(|attempts| {
            attempts.iter().any(|a| {
                matches!(
                    a["status"].as_str(),
                    Some("running" | "completed" | "dry_run")
                )
            })
        })
    {
        return None;
    }
    record["status"] = json!("stalled_no_leg");
    record["reason"] = json!("stalled_no_leg");
    record["ended"] = json!(now);
    record["exit_code"] = json!(6);
    Some(
        json!({"rule":"run-never-launched","kind":"alert","run_id":record["run_id"],
        "recipient":record["parent_session"].as_str()
            .or_else(|| record["completion"]["parent_session"].as_str()).unwrap_or("routing"),
        "reason":"STALLED-NO-LEG: run never launched a leg within bound"}),
    )
}

pub(crate) fn sweep(state: &Path, now: f64, bound: f64) -> Result<usize, String> {
    let mut count = 0;
    for snapshot in crate::records::list_runs(state) {
        let id = snapshot["run_id"].as_str().ok_or("invalid run id")?;
        let dir = pg_worker_core::run::record::run_dir(state, id).map_err(|_| "invalid run id")?;
        let mut current = crate::records::read_record(state, &snapshot["run_id"])
            .map_err(|_| "no-leg watch: record unreadable")?;
        if let Some(event) = expire(&mut current, now, bound) {
            pg_worker_core::run::record_io::atomic_write_private(
                &state.join(format!("watchdog/outbox/no-leg-{id}.json")),
                event.to_string().as_bytes(),
            )
            .map_err(|_| "no-leg watch: notice write failed")?;
            current
                .as_object_mut()
                .unwrap()
                .remove("_completion_acknowledged");
            pg_worker_core::run::record_io::atomic_write_private(
                &dir.join("record.json"),
                current.to_string().as_bytes(),
            )
            .map_err(|_| "no-leg watch: record write failed")?;
            count += 1;
        }
    }
    Ok(count)
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn std::io::Write) -> Result<(), String> {
    if !args.is_empty() {
        return Err("usage: pg-spawnd run-watch".into());
    }
    let state = crate::state::resolve_state(None, |k| std::env::var_os(k))?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "invalid clock")?
        .as_secs_f64();
    let bound: f64 = std::env::var("PLANGRAPH_NO_LEG_TIMEOUT_S")
        .unwrap_or_else(|_| "600".into())
        .parse()
        .map_err(|_| "invalid no-leg timeout")?;
    if !bound.is_finite() || bound <= 0.0 {
        return Err("invalid no-leg timeout".into());
    }
    let count = sweep(&state, now, bound)?;
    writeln!(out, "No-leg watch: {count} notices").map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timer_persists_state_and_does_not_repeat_consumed_notice() {
        let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        let path = root.join("runs/run-fixture/record.json");
        pg_worker_core::run::record_io::atomic_write_private(
            &path,
            json!({"run_id":"run-fixture","status":"running","started":1})
                .to_string()
                .as_bytes(),
        )
        .unwrap();
        assert_eq!(sweep(&root, 601.0, 600.0).unwrap(), 1);
        let saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["status"], "stalled_no_leg");
        std::fs::remove_file(root.join("watchdog/outbox/no-leg-run-fixture.json")).unwrap();
        assert_eq!(sweep(&root, 999.0, 600.0).unwrap(), 0);
        assert_eq!(
            std::fs::read_dir(root.join("watchdog/outbox"))
                .unwrap()
                .count(),
            0
        );
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn no_leg_after_bound_is_terminal_and_notifies_once() {
        let mut run = json!({"run_id":"run-fixture","status":"running","started":100,
            "parent_session":"parent-fixture","job_id":"queued-job", "attempts":[{"status":"queued"}]});
        assert!(expire(&mut run, 699.0, 600.0).is_none());
        let event = expire(&mut run, 700.0, 600.0).unwrap();
        assert_eq!(run["status"], "stalled_no_leg");
        assert_eq!(event["recipient"], "parent-fixture");
        assert!(expire(&mut run, 900.0, 600.0).is_none());
        for evidence in [
            json!({"pid":123}),
            json!({"launched_leg":{"id":"leg"}}),
            json!({"attempts":[{"status":"running"}]}),
        ] {
            let mut live = json!({"run_id":"run-live","status":"running","started":100});
            live.as_object_mut()
                .unwrap()
                .extend(evidence.as_object().unwrap().clone());
            assert!(expire(&mut live, 900.0, 600.0).is_none());
        }
    }
}
