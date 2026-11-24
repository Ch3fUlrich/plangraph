//! Writer sessions the fleet launcher started (hand lanes), seen as feeder runs so a hand lane
//! and a feeder run never overlap: each counts toward its host's writers and holds its items.
//! A session the spawner already lists as a run (the launcher registers one under the same
//! `run_id`) is that run's business; only sessions the spawner does not list are added here.
use super::probe::capture;
use super::{Env, Value};
use serde_json::json;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime};

/// `run_id` prefix of a lane run: lets decoding tell a lane from a spawner run.
pub(super) const LANE_RUN_PREFIX: &str = "lane:";
/// A job state file this old no longer proves its session is alive; the Claude roster decides.
const STATE_FRESH: Duration = Duration::from_secs(6 * 3600);
const ROSTER_TIMEOUT: Duration = Duration::from_secs(5);

/// The launcher's session records, `$ROUTING_STATE_DIR/launch/sessions` like the launcher itself.
pub(super) fn sessions_dir(home: &Path, env: &Env) -> PathBuf {
    env.get("ROUTING_STATE_DIR")
        .filter(|d| !d.is_empty())
        .map_or_else(|| home.join(".local/state/routing"), PathBuf::from)
        .join("launch/sessions")
}

/// The Claude job directories, under `$CLAUDE_CONFIG_DIR` when set.
pub(super) fn jobs_dir(home: &Path, env: &Env) -> PathBuf {
    env.get("CLAUDE_CONFIG_DIR")
        .filter(|d| !d.is_empty())
        .map_or_else(|| home.join(".claude"), PathBuf::from)
        .join("jobs")
}

/// What `claude agents --json --all` lists, or `None` when the roster cannot be read.
pub(super) fn claude_roster() -> Option<Vec<Value>> {
    let out = capture(
        Command::new("claude").args(["agents", "--json", "--all"]),
        ROSTER_TIMEOUT,
    )
    .ok()?;
    serde_json::from_slice(&out).ok()
}

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

#[cfg(unix)]
pub(super) fn pid_alive(pid: u64) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}
#[cfg(not(unix))]
pub(super) fn pid_alive(_: u64) -> bool {
    false
}

/// Where the lane evidence comes from; the roster and pid probes are injected for tests.
pub(super) struct Evidence<'a> {
    pub sessions: &'a Path,
    pub jobs: &'a Path,
    pub now: SystemTime,
    /// Run ids the spawner lists already (any status): those sessions are not read again.
    pub listed: &'a BTreeSet<String>,
    pub roster: &'a dyn Fn() -> Option<Vec<Value>>,
    pub pid_alive: &'a dyn Fn(u64) -> bool,
}

/// One synthetic spawner-style run row per live writer session the spawner does not list.
/// Alive means: the job's state is not terminal and either changed within six hours or the
/// Claude roster still lists the session (an unreadable roster keeps the slot); a session with
/// no job record is alive while its recorded pid is. A card naming plan items becomes the
/// run's item (the first key) and `item_keys`, so those items are never picked.
pub(super) fn live_lane_runs(ev: &Evidence) -> Vec<Value> {
    let Ok(entries) = fs::read_dir(ev.sessions) else {
        return vec![];
    };
    let mut roster: Option<Option<Vec<Value>>> = None;
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
        let (Some(name), Some(host)) = (
            record["session_name"].as_str(),
            record["host"].as_str().filter(|h| !h.is_empty()),
        ) else {
            continue;
        };
        if record["name"].as_str() != Some("writer")
            || record["run_id"]
                .as_str()
                .is_some_and(|id| ev.listed.contains(id))
        {
            continue;
        }
        let job = record["job_id"]
            .as_str()
            .filter(|j| !j.is_empty() && !j.contains(['/', '.']));
        let state_file = job.map(|j| ev.jobs.join(j).join("state.json"));
        let state = state_file
            .as_ref()
            .and_then(|f| fs::read_to_string(f).ok())
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|s| s["state"].as_str().map(str::to_owned));
        let live = match state {
            Some(state) if !live_state(&state) => false,
            Some(_) => {
                let fresh = state_file
                    .as_ref()
                    .and_then(|f| fs::metadata(f).ok())
                    .and_then(|m| m.modified().ok())
                    .is_some_and(|at| ev.now.duration_since(at).unwrap_or_default() <= STATE_FRESH);
                fresh || {
                    let listed = roster.get_or_insert_with(|| (ev.roster)());
                    let sid = record["session_id"].as_str().unwrap_or_default();
                    listed.as_ref().is_none_or(|agents| {
                        agents.iter().any(|a| {
                            a["sessionId"].as_str() == Some(sid)
                                && a["state"].as_str().is_none_or(live_state)
                        })
                    })
                }
            }
            None => record["pid"]
                .as_u64()
                .is_some_and(|pid| (ev.pid_alive)(pid)),
        };
        if !live {
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

    fn session(dir: &Path, name: &str, extra: Value) {
        let mut rec = json!({"name":"writer","session_name":name,"host":"workstation","ts":1000});
        for (k, v) in extra.as_object().unwrap() {
            rec[k] = v.clone();
        }
        fs::write(dir.join(format!("{name}.json")), rec.to_string()).unwrap();
    }
    fn job(dir: &Path, job: &str, state: &str) -> PathBuf {
        fs::create_dir_all(dir.join(job)).unwrap();
        let file = dir.join(job).join("state.json");
        fs::write(&file, json!({"state":state}).to_string()).unwrap();
        file
    }
    fn age(file: &Path, secs: u64) {
        let at = SystemTime::now() - Duration::from_secs(secs);
        File::options()
            .write(true)
            .open(file)
            .unwrap()
            .set_modified(at)
            .unwrap();
    }
    fn ids(rows: &[Value]) -> Vec<&str> {
        rows.iter().map(|r| r["run_id"].as_str().unwrap()).collect()
    }
    fn evidence<'a>(
        sessions: &'a Path,
        jobs: &'a Path,
        listed: &'a BTreeSet<String>,
        roster: &'a dyn Fn() -> Option<Vec<Value>>,
        pid_alive: &'a dyn Fn(u64) -> bool,
    ) -> Evidence<'a> {
        Evidence {
            sessions,
            jobs,
            now: SystemTime::now(),
            listed,
            roster,
            pid_alive,
        }
    }

    #[test]
    fn live_writer_sessions_become_runs_and_dead_or_other_ones_do_not() {
        let d = tempfile::tempdir().unwrap();
        let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
        fs::create_dir_all(&sessions).unwrap();
        session(
            &sessions,
            "writer-1",
            json!({"job_id":"aaa","card":"plangraph:ONE batch7"}),
        );
        session(
            &sessions,
            "writer-2",
            json!({"job_id":"bbb","card":"lane-name-only"}),
        );
        session(
            &sessions,
            "writer-3",
            json!({"job_id":"ccc","card":"plangraph:DONE"}),
        );
        session(
            &sessions,
            "writer-4",
            json!({"job_id":"ddd","card":"plangraph:STOPPED"}),
        );
        session(
            &sessions,
            "writer-7",
            json!({"job_id":"missing","card":"plangraph:NOJOB"}),
        );
        session(
            &sessions,
            "hostprobe-6",
            json!({"name":"hostprobe","job_id":"fff"}),
        );
        job(&jobs, "aaa", "working");
        job(&jobs, "bbb", "working");
        job(&jobs, "ccc", "done");
        job(&jobs, "ddd", "stopped");
        job(&jobs, "fff", "working");
        fs::write(sessions.join("junk.json"), "not json").unwrap();
        let none = BTreeSet::new();
        let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| None, &|_| false));
        assert_eq!(ids(&rows), ["lane:writer-1", "lane:writer-2"]);
        assert_eq!(rows[0]["item_key"], "plangraph:ONE");
        assert_eq!(rows[0]["host"], "workstation");
        assert_eq!(rows[1]["item_key"], "");
    }

    #[test]
    fn an_old_state_file_keeps_its_slot_while_the_roster_lists_the_session() {
        let d = tempfile::tempdir().unwrap();
        let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
        fs::create_dir_all(&sessions).unwrap();
        for (n, sid) in [("writer-1", "s1"), ("writer-2", "s2"), ("writer-3", "s3")] {
            session(
                &sessions,
                n,
                json!({"job_id":format!("j{n}"),"session_id":sid}),
            );
            age(&job(&jobs, &format!("j{n}"), "working"), 7 * 3600);
        }
        let none = BTreeSet::new();
        let roster = || {
            Some(vec![
                json!({"sessionId":"s1","state":"working"}),
                json!({"sessionId":"s2","state":"done"}),
            ])
        };
        // Listed and working stays; listed done and absent from the roster go.
        let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &roster, &|_| false));
        assert_eq!(ids(&rows), ["lane:writer-1"]);
        // An unreadable roster is no evidence of death: every old job keeps its slot.
        let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| None, &|_| false));
        assert_eq!(
            ids(&rows),
            ["lane:writer-1", "lane:writer-2", "lane:writer-3"]
        );
    }

    #[test]
    fn a_session_with_only_a_pid_lives_while_that_pid_does() {
        let d = tempfile::tempdir().unwrap();
        let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
        fs::create_dir_all(&sessions).unwrap();
        session(
            &sessions,
            "writer-1",
            json!({"pid":4242,"card":"plangraph:PIDLANE"}),
        );
        session(&sessions, "writer-2", json!({"pid":4343}));
        session(&sessions, "writer-3", json!({}));
        let none = BTreeSet::new();
        let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| None, &|p| p == 4242));
        assert_eq!(ids(&rows), ["lane:writer-1"]);
        assert_eq!(rows[0]["item_key"], "plangraph:PIDLANE");
        assert!(pid_alive(u64::from(std::process::id())));
    }

    #[test]
    fn a_session_the_spawner_lists_is_not_counted_twice() {
        let d = tempfile::tempdir().unwrap();
        let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
        fs::create_dir_all(&sessions).unwrap();
        session(
            &sessions,
            "writer-1",
            json!({"job_id":"aaa","run_id":"run-1"}),
        );
        session(
            &sessions,
            "writer-2",
            json!({"job_id":"bbb","run_id":"run-2"}),
        );
        job(&jobs, "aaa", "working");
        job(&jobs, "bbb", "working");
        let listed = BTreeSet::from(["run-1".to_string()]);
        let rows = live_lane_runs(&evidence(&sessions, &jobs, &listed, &|| None, &|_| false));
        assert_eq!(ids(&rows), ["lane:writer-2"]);
    }

    #[test]
    fn the_launcher_state_directory_and_claude_config_directory_are_honoured() {
        let home = Path::new("/home/u");
        let mut env = Env::new();
        assert_eq!(
            sessions_dir(home, &env),
            Path::new("/home/u/.local/state/routing/launch/sessions")
        );
        assert_eq!(jobs_dir(home, &env), Path::new("/home/u/.claude/jobs"));
        env.insert("ROUTING_STATE_DIR".into(), "/srv/routing".into());
        env.insert("CLAUDE_CONFIG_DIR".into(), "/srv/claude".into());
        assert_eq!(
            sessions_dir(home, &env),
            Path::new("/srv/routing/launch/sessions")
        );
        assert_eq!(jobs_dir(home, &env), Path::new("/srv/claude/jobs"));
    }
}
