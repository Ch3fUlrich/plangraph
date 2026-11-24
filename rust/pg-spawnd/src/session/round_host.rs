//! Real effects of the round watchdog on the executing host (Linux /proc, signals, outbox).

use super::round_watch::{Host, Owner};
use pg_worker_core::run::{
    group::{stop_group, Identity, StopOutcome},
    group_unix::{proc_start_time, Roots, UnixGroup},
    record_io::atomic_write_private,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{fs, io, path::PathBuf, time::Duration};

pub struct LocalHost {
    pub state: PathBuf,
    /// The harness session id; every command shell of the session carries it in its environment.
    pub session_id: String,
    pub roots: Roots,
}

impl LocalHost {
    pub fn new(state: PathBuf, session_id: String) -> Self {
        Self {
            state,
            session_id,
            roots: Roots::default(),
        }
    }
}

fn environ_has(environ: &[u8], session_id: &str) -> bool {
    let wanted = format!("CLAUDE_CODE_SESSION_ID={session_id}");
    environ
        .split(|b| *b == 0)
        .any(|entry| entry == wanted.as_bytes())
}

/// The command text appears in the shell's argv, joined by NUL bytes.
pub(super) fn cmdline_runs(cmdline: &[u8], command: &str) -> bool {
    let text = String::from_utf8_lossy(cmdline);
    let flat = text.replace('\0', " ");
    let needle: String = command.split_whitespace().collect::<Vec<_>>().join(" ");
    !needle.is_empty()
        && flat
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .contains(&needle)
}

impl Host for LocalHost {
    fn resolve_owner(&self, _session: &str, command: &str) -> Option<Owner> {
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
            let (Ok(environ), Ok(cmdline)) =
                (fs::read(dir.join("environ")), fs::read(dir.join("cmdline")))
            else {
                continue;
            };
            if !environ_has(&environ, &self.session_id) || !cmdline_runs(&cmdline, command) {
                continue;
            }
            // Only a group leader owns a command; its group is what is ended.
            let stat = fs::read_to_string(dir.join("stat")).ok()?;
            let after = stat
                .rsplit_once(')')?
                .1
                .split_whitespace()
                .collect::<Vec<_>>();
            if after.get(2).and_then(|g| g.parse::<u32>().ok()) == Some(pid) {
                found.push(Owner {
                    pid,
                    start_time: proc_start_time(&self.roots.proc, pid),
                });
            }
        }
        // Zero or several matches prove no owner: nothing is ended on a guess.
        (found.len() == 1).then(|| found.remove(0))
    }

    fn stop(&self, owner: &Owner) -> bool {
        let group = UnixGroup {
            identity: Identity {
                pid: owner.pid,
                start_time: owner.start_time,
                group: None,
            },
            roots: Roots {
                proc: self.roots.proc.clone(),
                cgroup: self.roots.cgroup.clone(),
            },
        };
        stop_group(&group, Duration::from_secs(5), Duration::from_secs(5)) == StopOutcome::Empty
    }

    fn notify(&self, recipient: &str, key: &str, line: &str) -> io::Result<()> {
        let digest = Sha256::digest(format!("{recipient}|{key}"));
        let name: String = digest.iter().take(16).map(|b| format!("{b:02x}")).collect();
        let path = self
            .state
            .join("watchdog/outbox")
            .join(format!("round-wait-{name}.json"));
        if path.exists() {
            return Ok(());
        }
        let event = json!({"rule": "round-wait-timeout", "kind": "alert",
            "recipient": recipient, "key": key, "text": line});
        atomic_write_private(&path, event.to_string().as_bytes())
    }
}
