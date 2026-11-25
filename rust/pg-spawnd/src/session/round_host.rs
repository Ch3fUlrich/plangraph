//! Real effects of the round watchdog on the executing host (Linux /proc, signals, outbox).

use super::{
    round_wait::TaskRef,
    round_watch::{Host, Owner, Stop},
};
use pg_worker_core::run::{
    group::{stop_group, Identity, RunGroup, StopOutcome},
    group_unix::{parse_stat, proc_start_time, Roots, UnixGroup},
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
    /// Sends one signal to one member of the owned group; true only when the kernel
    /// accepted it for that very process.
    pub send_signal: fn(&Member, i32) -> bool,
    /// The `claude` binary of `claude stop`; the environment's, then the PATH's when none.
    pub claude_bin: Option<PathBuf>,
}

/// A process of the owned group as it was seen: the identity a signal must still match.
#[derive(Clone, Debug)]
pub struct Member {
    pub pid: u32,
    pub start_time: u64,
    pub group: u32,
}

fn identity_matches(proc: &Path, member: &Member) -> bool {
    fs::read_to_string(proc.join(member.pid.to_string()).join("stat"))
        .ok()
        .and_then(|text| parse_stat(&text))
        .is_some_and(|(state, group, start)| {
            !matches!(state, 'Z' | 'X') && group == member.group && start == member.start_time
        })
}

/// Pin the process with a pidfd, check it is still the member that was seen, then signal
/// through the pidfd: a pid reused or a process that left the group is never signalled.
pub fn signal_member(member: &Member, signal: i32) -> bool {
    if member.pid <= 1 || member.pid > i32::MAX as u32 {
        return false;
    }
    // SAFETY: pidfd_open takes a pid and flags and returns a descriptor or -1.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, member.pid as libc::c_int, 0) };
    if fd < 0 {
        return false;
    }
    let fd = fd as libc::c_int;
    let sent = identity_matches(Path::new("/proc"), member)
        // SAFETY: pidfd_send_signal takes the descriptor, a signal and a null siginfo.
        && unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                fd,
                signal,
                std::ptr::null::<libc::c_void>(),
                0,
            )
        } == 0;
    // SAFETY: fd is the descriptor opened above.
    unsafe { libc::close(fd) };
    sent
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
        // The kernel must accept a signal for a member that was seen alive: that
        // acceptance is the evidence of the watchdog's stop. A member that exited
        // between the scan and the signal is not a stop of ours.
        let accepted = members
            .iter()
            .filter_map(|pid| {
                let text =
                    fs::read_to_string(self.roots.proc.join(pid.to_string()).join("stat")).ok()?;
                let (_, group_id, start_time) = parse_stat(&text)?;
                Some(Member {
                    pid: *pid,
                    start_time,
                    group: group_id,
                })
            })
            .filter(|member| (self.send_signal)(member, libc::SIGTERM))
            .count();
        if accepted == 0 {
            // No signal reached anyone: members that remain are not stopped, and nothing
            // may be recorded as signalled.
            let left = group.members().unwrap_or_default();
            return match (left.is_empty(), signalled) {
                (true, true) => Stop::Ended,
                (true, false) => Stop::Gone,
                (false, _) => Stop::Unsignalled,
            };
        }
        match stop_group(&group, Duration::from_secs(5), Duration::from_secs(5)) {
            StopOutcome::Empty => Stop::Ended,
            StopOutcome::Survivors(_) => Stop::Survivors,
        }
    }

    fn mark_stop(&self, task: &TaskRef, key: &str) -> bool {
        use std::io::Write;
        let Some(path) = task.output_path.as_deref() else {
            return false;
        };
        let line = format!(
            "\n[round-watch] ending this command: the writer waited on it over 15 minutes (stop {})\n",
            mark_id(key)
        );
        fs::OpenOptions::new()
            .append(true)
            .open(path)
            .and_then(|mut file| {
                file.write_all(line.as_bytes())?;
                file.sync_all()
            })
            .is_ok()
    }

    fn has_mark(&self, task: &TaskRef, key: &str) -> bool {
        use std::io::{Read, Seek, SeekFrom};
        let Some(path) = task.output_path.as_deref() else {
            return false;
        };
        let Ok(mut file) = fs::File::open(path) else {
            return false;
        };
        let len = file.metadata().map_or(0, |m| m.len());
        let mut tail = Vec::new();
        let ok = file
            .seek(SeekFrom::Start(len.saturating_sub(256 * 1024)))
            .is_ok()
            && file.read_to_end(&mut tail).is_ok();
        ok && String::from_utf8_lossy(&tail).contains(&format!("(stop {})", mark_id(key)))
    }

    fn pending(&self, recipient: &str, key: &str) -> bool {
        self.outbox_file(recipient, key).1.exists()
    }

    fn end_session(&self, _session: &str) -> bool {
        // `claude stop` takes the short job id: the first eight characters of the session id.
        let job: String = self.session_id.chars().take(8).collect();
        let bin = self.claude_bin.clone().unwrap_or_else(|| {
            std::env::var("PLANGRAPH_CLAUDE_BIN")
                .ok()
                .filter(|v| !v.is_empty())
                .map_or_else(|| "claude".into(), PathBuf::from)
        });
        let run = |args: &[&str]| {
            std::process::Command::new(&bin)
                .args(args)
                .stdin(std::process::Stdio::null())
                .output()
                .ok()
                .filter(|out| out.status.success())
        };
        if run(&["stop", &job]).is_none() {
            return false;
        }
        // Exit zero is not a stopped session: `claude agents` must no longer list it live
        // (the same confirmation Sessions._ensure_stopped uses). A failing poll is unconfirmed.
        let Some(listing) = run(&["agents", "--json"]) else {
            return false;
        };
        let text = String::from_utf8_lossy(&listing.stdout);
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
