//! Real effects of the round watchdog on the executing host (Linux /proc, signals, outbox).

pub use super::round_signal::{signal_member, Member};
use super::{
    round_wait::TaskRef,
    round_watch::{Host, Owner, Sent, Stop},
};
use pg_worker_core::run::{
    group_unix::{parse_stat, proc_start_time, Roots},
    record_io::atomic_write_private,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs, io,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
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
    /// Sends one signal to one member of the owned group; true only when the kernel
    /// accepted it for that very process.
    pub send_signal: fn(&Member, i32) -> bool,
    /// The `claude` binary of `claude stop`; the environment's, then the PATH's when none.
    pub claude_bin: Option<PathBuf>,
}

impl LocalHost {
    pub fn new(state: PathBuf, session_id: String) -> Self {
        Self {
            state,
            session_id,
            roots: Roots::default(),
            launch_slack_ms: Some(LAUNCH_SLACK_MS),
            send_signal: signal_member,
            claude_bin: None,
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

    /// Every live member of the process group, identity and group read once per process.
    fn scan_members(&self, group: u32) -> Vec<Member> {
        let Ok(entries) = fs::read_dir(&self.roots.proc) else {
            return Vec::new();
        };
        entries
            .flatten()
            .filter_map(|entry| {
                let pid: u32 = entry.file_name().to_str()?.parse().ok()?;
                let text = fs::read_to_string(entry.path().join("stat")).ok()?;
                let (state, pgrp, start_time) = parse_stat(&text)?;
                (pgrp == group && !matches!(state, 'Z' | 'X')).then_some(Member {
                    pid,
                    start_time,
                    group: pgrp,
                })
            })
            .collect()
    }

    /// The group of the owner when it is still the owner's: a live leader with another start
    /// time or output is a reused pid and owns nothing; a dead leader with members left is
    /// its own group, whose id cannot be reused yet.
    fn owned_group(&self, owner: &Owner) -> Option<u32> {
        let alive = proc_start_time(&self.roots.proc, owner.pid);
        // The start time is the identity of the leader. Where it writes is not: a command
        // may redirect its output after a signal and still be the command that was found.
        (alive.is_none() || alive == owner.start_time).then_some(owner.pid)
    }

    /// Signal every member of the group through its pidfd; how many the kernel accepted.
    fn signal_group(&self, group: u32, signal: i32) -> usize {
        self.scan_members(group)
            .iter()
            .filter(|member| (self.send_signal)(member, signal))
            .count()
    }

    /// Wait for the group to empty, up to `limit`.
    fn emptied(&self, group: u32, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        loop {
            if self.scan_members(group).is_empty() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }
}

/// The short id of one stop action in the owner's output.
fn mark_id(key: &str) -> String {
    Sha256::digest(key.as_bytes())
        .iter()
        .take(6)
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `SID_RE` of the session store: one alphanumeric, then up to 63 of `[A-Za-z0-9._-]`.
fn plain_session_id(id: &str) -> bool {
    let mut chars = id.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && id.len() <= 64
        && chars.all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
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

/// Device and inode of what the path (a /proc descriptor link, or a path) reaches.
fn inode_of(path: &Path) -> Option<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    fs::metadata(path).ok().map(|m| (m.dev(), m.ino()))
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
                output: inode_of(&dir.join("fd/1")),
            });
        }
        // Zero or several matches prove no owner: nothing is ended on a guess.
        (found.len() == 1).then(|| found.remove(0))
    }

    fn signal(&self, owner: &Owner, _task: &TaskRef) -> Sent {
        let Some(group) = self.owned_group(owner) else {
            return Sent::Gone;
        };
        if self.scan_members(group).is_empty() {
            return Sent::Gone;
        }
        // The kernel must accept the signal for a member seen alive; a member gone before
        // the signal is no stop of ours.
        if self.signal_group(group, libc::SIGTERM) > 0 {
            return Sent::Accepted;
        }
        if self.scan_members(group).is_empty() {
            Sent::Gone
        } else {
            Sent::Refused
        }
    }

    fn finish_stop(&self, owner: &Owner, _task: &TaskRef) -> Stop {
        let Some(group) = self.owned_group(owner) else {
            return Stop::Ended;
        };
        // Escalation goes through the same verified signalling, member by member.
        for (signal, limit) in [
            (None, Duration::from_secs(5)),
            (Some(libc::SIGKILL), Duration::from_secs(5)),
        ] {
            if let Some(signal) = signal {
                self.signal_group(group, signal);
            }
            if self.emptied(group, limit) {
                return Stop::Ended;
            }
        }
        Stop::Survivors
    }

    fn group_gone(&self, owner: &Owner, _task: &TaskRef) -> bool {
        self.owned_group(owner)
            .is_none_or(|group| self.scan_members(group).is_empty())
    }

    fn mark_stop(&self, owner: &Owner, _task: &TaskRef, key: &str) -> bool {
        use std::io::Write;
        let dir = self.roots.proc.join(owner.pid.to_string());
        let Some(expected) = owner.output else {
            return false;
        };
        if proc_start_time(&self.roots.proc, owner.pid) != owner.start_time
            || inode_of(&dir.join("fd/1")) != Some(expected)
        {
            return false;
        }
        let line = format!(
            "\n[round-watch] ending this command: the writer waited on it over 15 minutes (stop {})\n",
            mark_id(key)
        );
        // Written through the owner's own descriptor, never through the recorded path: the
        // file reached is the one the command writes to, checked by inode after the open.
        let Ok(mut file) = fs::OpenOptions::new().append(true).open(dir.join("fd/1")) else {
            return false;
        };
        let same = {
            use std::os::unix::fs::MetadataExt;
            file.metadata()
                .is_ok_and(|m| (m.dev(), m.ino()) == expected)
        };
        same && file.write_all(line.as_bytes()).is_ok() && file.sync_all().is_ok()
    }

    fn pending(&self, recipient: &str, key: &str) -> bool {
        self.outbox_file(recipient, key).1.exists()
    }

    fn end_session(&self, _session: &str) -> bool {
        self.end_session_with_sleeper(std::thread::sleep)
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

impl LocalHost {
    pub(super) fn end_session_with_sleeper(&self, mut sleep: impl FnMut(Duration)) -> bool {
        let bin = self.claude_bin.clone().unwrap_or_else(|| {
            std::env::var("PLANGRAPH_CLAUDE_BIN")
                .ok()
                .filter(|v| !v.is_empty())
                .map_or_else(|| "claude".into(), PathBuf::from)
        });
        self.end_session_with(|args| {
            let mut command = Command::new(&bin);
            command
                .args(args)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            pg_worker_core::stall::spawn_retrying_with(|| command.spawn(), &mut sleep)
                .and_then(Child::wait_with_output)
                .ok()
                .filter(|out| out.status.success())
                .map(|out| out.stdout)
        })
    }

    pub(super) fn end_session_with(&self, mut run: impl FnMut(&[&str]) -> Option<Vec<u8>>) -> bool {
        // `claude stop` takes the short job id: the first eight characters of the session id.
        let job: String = self.session_id.chars().take(8).collect();
        if run(&["stop", &job]).is_none() {
            return false;
        }
        // Exit zero is not a stopped session: `claude agents` must no longer list it live
        // (the same confirmation Sessions._ensure_stopped uses). A failing poll is unconfirmed.
        let Some(listing) = run(&["agents", "--json"]) else {
            return false;
        };
        let text = String::from_utf8_lossy(&listing);
        let rows: serde_json::Value = if text.trim().is_empty() {
            json!([])
        } else {
            match serde_json::from_str(text.trim()) {
                Ok(rows) => rows,
                Err(_) => return false,
            }
        };
        let Some(rows) = rows.as_array() else {
            return false;
        };
        // The same strict contract as `Sessions._agents_rows_strict`: every row is an object
        // and its id, when present, is a plain session id; anything else is unconfirmed.
        let well_formed = |row: &serde_json::Value| {
            row.is_object()
                && match row.get("sessionId") {
                    None | Some(serde_json::Value::Null) => true,
                    Some(serde_json::Value::String(id)) => plain_session_id(id),
                    Some(_) => false,
                }
        };
        rows.iter().all(well_formed)
            && !rows.iter().any(|row| {
                row["sessionId"] == json!(self.session_id)
                    && !["finished", "stopped", "done", "failed"]
                        .contains(&row["state"].as_str().unwrap_or(""))
            })
    }
}
