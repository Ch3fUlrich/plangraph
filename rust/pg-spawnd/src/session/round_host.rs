//! Real effects of the round watchdog on the executing host (Linux /proc, signals, outbox).

use super::{
    round_wait::TaskRef,
    round_watch::{Host, Owner, Stop},
};
use pg_worker_core::run::{
    group::{stop_group, Identity, RunGroup, StopOutcome},
    group_unix::{proc_start_time, Roots, UnixGroup},
    record_io::atomic_write_private,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::Duration,
};

/// How far a process may have started before the launching call or after its result.
pub const LAUNCH_SLACK_MS: i64 = 30_000;

pub struct LocalHost {
    pub state: PathBuf,
    /// The harness session id; every command shell of the session carries it in its environment.
    pub session_id: String,
    pub roots: Roots,
    /// Launch window slack; none skips the check (fixtures that start processes now
    /// under a transcript of another time).
    pub launch_slack_ms: Option<i64>,
}

impl LocalHost {
    pub fn new(state: PathBuf, session_id: String) -> Self {
        Self {
            state,
            session_id,
            roots: Roots::default(),
            launch_slack_ms: Some(LAUNCH_SLACK_MS),
        }
    }

    fn outbox_file(&self, recipient: &str, key: &str) -> (String, PathBuf) {
        let digest = Sha256::digest(format!("{recipient}|{key}"));
        let name: String = digest.iter().take(16).map(|b| format!("{b:02x}")).collect();
        let file = self
            .state
            .join("watchdog/outbox")
            .join(format!("round-wait-{name}.json"));
        (name, file)
    }

    fn group(&self, owner: &Owner) -> UnixGroup {
        UnixGroup {
            identity: Identity {
                pid: owner.pid,
                start_time: owner.start_time,
                group: None,
            },
            roots: Roots {
                proc: self.roots.proc.clone(),
                cgroup: self.roots.cgroup.clone(),
            },
        }
    }
}

fn environ_has(environ: &[u8], session_id: &str) -> bool {
    let wanted = format!("CLAUDE_CODE_SESSION_ID={session_id}");
    environ
        .split(|b| *b == 0)
        .any(|entry| entry == wanted.as_bytes())
}

/// The file a command shell writes to: the harness points its stdout at the task's output.
fn output_file(proc_dir: &Path) -> Option<PathBuf> {
    fs::read_link(proc_dir.join("fd/1")).ok()
}

/// Wall-clock start of a process, from the boot time and its start tick.
fn started_ms(root: &Path, pid: u32) -> Option<i64> {
    let ticks = proc_start_time(root, pid)?;
    let boot = fs::read_to_string(root.join("stat"))
        .ok()?
        .lines()
        .find_map(|l| l.strip_prefix("btime ")?.trim().parse::<i64>().ok())?;
    // SAFETY: sysconf takes a constant and returns a number.
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1);
    Some(boot * 1000 + ticks as i64 * 1000 / hz)
}

fn leader_of(proc_dir: &Path) -> Option<u32> {
    let stat = fs::read_to_string(proc_dir.join("stat")).ok()?;
    let after: Vec<&str> = stat.rsplit_once(')')?.1.split_whitespace().collect();
    after.get(2)?.parse().ok()
}

impl Host for LocalHost {
    fn resolve_owner(&self, _session: &str, task: &TaskRef) -> Option<Owner> {
        let wanted = PathBuf::from(task.output_path.as_deref()?);
        let mut found = Vec::new();
        for entry in fs::read_dir(&self.roots.proc).ok()?.flatten() {
            let Some(pid) = entry
                .file_name()
                .to_str()
                .and_then(|n| n.parse::<u32>().ok())
            else {
                continue;
            };
            let dir = entry.path();
            let Ok(environ) = fs::read(dir.join("environ")) else {
                continue;
            };
            if !environ_has(&environ, &self.session_id)
                || output_file(&dir).as_deref() != Some(wanted.as_path())
            {
                continue;
            }
            // Only a group leader owns a command; its group is what is ended.
            if leader_of(&dir) != Some(pid) {
                continue;
            }
            // The process was started by the call that launched the task.
            if let Some(slack) = self.launch_slack_ms {
                let Some(start) = started_ms(&self.roots.proc, pid) else {
                    continue;
                };
                if start < task.launched_from_ms - slack || start > task.launched_to_ms + slack {
                    continue;
                }
            }
            found.push(Owner {
                pid,
                start_time: proc_start_time(&self.roots.proc, pid),
            });
        }
        // Zero or several matches prove no owner: nothing is ended on a guess.
        (found.len() == 1).then(|| found.remove(0))
    }

    fn stop(&self, owner: &Owner, task: &TaskRef, signalled: bool) -> Stop {
        let dir = self.roots.proc.join(owner.pid.to_string());
        let group = self.group(owner);
        let members = group.members().unwrap_or_default();
        let leader_alive = proc_start_time(&self.roots.proc, owner.pid);
        let output = task.output_path.as_deref().map(PathBuf::from);
        let leader_ours = leader_alive.is_some()
            && leader_alive == owner.start_time
            && output.is_some()
            && output_file(&dir) == output;
        // A live process with another start time or output is a reused pid: not ours. A
        // dead leader with members left is its own group: its pid cannot be reused yet.
        if leader_alive.is_some() && !leader_ours {
            return Stop::Gone;
        }
        if members.is_empty() {
            // Nothing left to signal: it ended by itself, or after our earlier signal.
            return if signalled { Stop::Ended } else { Stop::Gone };
        }
        // Members were seen alive: this signal is the evidence of the watchdog's stop.
        group.terminate();
        match stop_group(&group, Duration::from_secs(5), Duration::from_secs(5)) {
            StopOutcome::Empty => Stop::Ended,
            StopOutcome::Survivors(_) => Stop::Survivors,
        }
    }

    fn pending(&self, recipient: &str, key: &str) -> bool {
        self.outbox_file(recipient, key).1.exists()
    }

    fn end_session(&self, _session: &str) -> bool {
        // `claude stop` takes the short job id: the first eight characters of the session id.
        let job: String = self.session_id.chars().take(8).collect();
        let bin = std::env::var("PLANGRAPH_CLAUDE_BIN")
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "claude".into());
        std::process::Command::new(bin)
            .args(["stop", &job])
            .stdin(std::process::Stdio::null())
            .output()
            .is_ok_and(|out| out.status.success())
    }

    fn notify(&self, recipient: &str, key: &str, line: &str) -> io::Result<()> {
        let (name, outbox) = self.outbox_file(recipient, key);
        // The sent marker outlives the delivery that removes the outbox file: a line is
        // queued once, never queued again after the bridge delivered it.
        let marker = self.state.join("round-watch/sent").join(&name);
        if marker.exists() {
            return Ok(());
        }
        if !outbox.exists() {
            let event = json!({"rule": "round-wait-timeout", "kind": "alert",
                "recipient": recipient, "key": key, "text": line});
            atomic_write_private(&outbox, event.to_string().as_bytes())?;
        }
        atomic_write_private(&marker, b"1")
    }
}
