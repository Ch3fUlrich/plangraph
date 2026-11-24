//! Writer sessions the fleet launcher started (hand lanes), seen as feeder runs so a hand lane
//! and a feeder run never overlap: each counts toward its host's writers and holds its item.
use super::Value;
use serde_json::json;
use std::fs;
use std::path::Path;
use std::time::{Duration, SystemTime};

/// `run_id` prefix of a lane run: lets decoding tell a lane from a spawner run.
pub(super) const LANE_RUN_PREFIX: &str = "lane:";
/// A job whose state file has not changed for this long is a dead session whose daemon never
/// wrote `done`; it stops holding a slot. Idle writers waiting on a review stay far below it.
const LANE_STALE: Duration = Duration::from_secs(6 * 3600);

/// Whether a job state counts as a live session: anything but a terminal state.
fn live_state(state: &str) -> bool {
    !matches!(state, "done" | "stopped" | "failed" | "")
}

/// The plan item keys a lane card names (the card may be a lane name, a key or both).
fn item_keys(card: &str) -> Vec<&str> {
    card.split(|c: char| c.is_whitespace() || c == ',')
        .filter(|t| t.starts_with("plangraph:"))
        .collect()
}

/// One synthetic spawner-style run row per live writer session of `sessions_dir`: its job in
/// `jobs_dir` is not terminal and was updated within six hours. A card naming plan items becomes
/// the run's item (the first key) and `item_keys`, so those items are never picked.
pub(super) fn live_lane_runs(sessions_dir: &Path, jobs_dir: &Path, now: SystemTime) -> Vec<Value> {
    let Ok(entries) = fs::read_dir(sessions_dir) else {
        return vec![];
    };
    let mut rows = vec![];
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Some(record) = fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        else {
            continue;
        };
        let (Some(name), Some(job), Some(host)) = (
            record["session_name"].as_str(),
            record["job_id"].as_str(),
            record["host"].as_str().filter(|h| !h.is_empty()),
        ) else {
            continue;
        };
        if record["name"].as_str() != Some("writer") || job.contains(['/', '.']) {
            continue;
        }
        let state_file = jobs_dir.join(job).join("state.json");
        let fresh = fs::metadata(&state_file)
            .and_then(|m| m.modified())
            .is_ok_and(|at| now.duration_since(at).unwrap_or_default() <= LANE_STALE);
        let live = fs::read_to_string(&state_file)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|s| s["state"].as_str().map(live_state))
            .unwrap_or(false);
        if !(fresh && live) {
            continue;
        }
        let keys = record["card"].as_str().map(item_keys).unwrap_or_default();
        rows.push(json!({
            "run_id": format!("{LANE_RUN_PREFIX}{name}"), "host": host, "status": "running",
            "role": "writer", "started": record["ts"],
            "item_key": keys.first().copied().unwrap_or(""),
            "item_keys": keys.iter().skip(1).collect::<Vec<_>>(),
        }));
    }
    rows.sort_by(|a, b| a["run_id"].as_str().cmp(&b["run_id"].as_str()));
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;

    fn session(dir: &Path, name: &str, job: &str, card: Value, role: &str) {
        let rec = json!({"name":role,"session_name":name,"job_id":job,"host":"workstation",
            "card":card,"ts":1000});
        fs::write(dir.join(format!("{name}.json")), rec.to_string()).unwrap();
    }
    fn job(dir: &Path, job: &str, state: &str) -> std::path::PathBuf {
        fs::create_dir_all(dir.join(job)).unwrap();
        let file = dir.join(job).join("state.json");
        fs::write(&file, json!({"state":state}).to_string()).unwrap();
        file
    }

    #[test]
    fn live_writer_sessions_become_runs_and_dead_or_other_ones_do_not() {
        let d = tempfile::tempdir().unwrap();
        let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
        fs::create_dir_all(&sessions).unwrap();
        session(
            &sessions,
            "writer-1",
            "aaa",
            json!("plangraph:ONE batch7"),
            "writer",
        );
        session(
            &sessions,
            "writer-2",
            "bbb",
            json!("lane-name-only"),
            "writer",
        );
        session(
            &sessions,
            "writer-3",
            "ccc",
            json!("plangraph:DONE"),
            "writer",
        );
        session(
            &sessions,
            "writer-4",
            "ddd",
            json!("plangraph:STOPPED"),
            "writer",
        );
        session(
            &sessions,
            "writer-5",
            "eee",
            json!("plangraph:OLD"),
            "writer",
        );
        session(&sessions, "hostprobe-6", "fff", json!(null), "hostprobe");
        session(
            &sessions,
            "writer-7",
            "missing",
            json!("plangraph:NOJOB"),
            "writer",
        );
        job(&jobs, "aaa", "working");
        job(&jobs, "bbb", "working");
        job(&jobs, "ccc", "done");
        job(&jobs, "ddd", "stopped");
        let old = job(&jobs, "eee", "working");
        job(&jobs, "fff", "working");
        let aged = SystemTime::now() - Duration::from_secs(7 * 3600);
        File::options()
            .write(true)
            .open(old)
            .unwrap()
            .set_modified(aged)
            .unwrap();
        fs::write(sessions.join("junk.json"), "not json").unwrap();
        let rows = live_lane_runs(&sessions, &jobs, SystemTime::now());
        let ids: Vec<_> = rows.iter().map(|r| r["run_id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["lane:writer-1", "lane:writer-2"]);
        assert_eq!(rows[0]["item_key"], "plangraph:ONE");
        assert_eq!(rows[0]["host"], "workstation");
        assert_eq!(rows[1]["item_key"], "");
    }
}
