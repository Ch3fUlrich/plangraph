//! Remote Control disappearance is an episode, with one durable routing notice.
use serde_json::{json, Value};
use std::collections::BTreeSet;

fn dropped(records: &[Value], actual: &[Value], alerted: &mut BTreeSet<String>) -> Vec<Value> {
    let mut events = Vec::new();
    let mut live = BTreeSet::new();
    for record in records {
        let Some(sid) = record["session_id"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        if record["require_remote_control"] != true || record["state"] != "running" {
            continue;
        }
        live.insert(sid.to_owned());
        let row = actual.iter().find(|row| row["sessionId"] == sid);
        let terminal = row.is_some_and(|r| {
            matches!(
                r["state"].as_str(),
                Some("done" | "finished" | "stopped" | "failed")
            )
        });
        let connected = row.is_some_and(|r| {
            r["bridgeSessionId"]
                .as_str()
                .is_some_and(|b| !b.trim().is_empty())
        });
        if connected || terminal {
            alerted.remove(sid);
            continue;
        }
        if alerted.insert(sid.to_owned()) {
            events.push(json!({"rule":"rc-drop","kind":"alert","session_id":sid,
                "recipient":record["parent"].as_str().filter(|p| !p.is_empty()).unwrap_or("routing"),
                "reason":"RC-DROPPED: required session bridge disappeared"}));
        }
    }
    alerted.retain(|sid| live.contains(sid));
    events
}

pub(crate) fn sweep(state: &std::path::Path, actual: &[Value]) -> Result<usize, String> {
    use sha2::{Digest, Sha256};
    let stamp = state.join("watchdog/rc-watch.json");
    let mut alerted = match std::fs::read(&stamp) {
        Ok(bytes) => {
            serde_json::from_slice(&bytes).map_err(|_| "RC watch: invalid episode state")?
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => BTreeSet::new(),
        Err(_) => return Err("RC watch: unreadable episode state".into()),
    };
    let mut records = Vec::new();
    let directory = state.join("sessions");
    if directory.exists() {
        for entry in std::fs::read_dir(directory).map_err(|_| "RC watch: sessions unreadable")? {
            let path = entry.map_err(|_| "RC watch: session unreadable")?.path();
            if path.extension().is_some_and(|e| e == "json") && !path.is_symlink() {
                let bytes = std::fs::read(path).map_err(|_| "RC watch: session unreadable")?;
                records
                    .push(serde_json::from_slice(&bytes).map_err(|_| "RC watch: invalid session")?);
            }
        }
    }
    let events = dropped(&records, actual, &mut alerted);
    for event in &events {
        let key = format!(
            "{:x}",
            Sha256::digest(event["session_id"].as_str().unwrap().as_bytes())
        );
        pg_worker_core::run::record_io::atomic_write_private(
            &state.join(format!("watchdog/outbox/rc-drop-{key}.json")),
            event.to_string().as_bytes(),
        )
        .map_err(|_| "RC watch: notice write failed")?;
    }
    pg_worker_core::run::record_io::atomic_write_private(
        &stamp,
        serde_json::to_vec(&alerted).unwrap().as_slice(),
    )
    .map_err(|_| "RC watch: state write failed")?;
    Ok(events.len())
}

fn bridge_rows() -> Result<Vec<Value>, String> {
    use std::{
        io::{Read, Seek, SeekFrom},
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    let mut output = tempfile::tempfile().map_err(|_| "RC watch: output unavailable")?;
    let executable = std::env::var_os("PLANGRAPH_CLAUDE_BIN").unwrap_or_else(|| {
        if cfg!(windows) {
            "claude.exe"
        } else {
            "claude"
        }
        .into()
    });
    let mut command = Command::new(executable);
    command
        .args(["agents", "--json"])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(
            output
                .try_clone()
                .map_err(|_| "RC watch: output unavailable")?,
        );
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|_| "RC watch: bridge query unavailable")?;
    let start = Instant::now();
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|_| "RC watch: bridge query failed")?
        {
            if !status.success() {
                return Err("RC watch: bridge query failed".into());
            }
            break;
        }
        if start.elapsed() >= Duration::from_secs(10) {
            #[cfg(unix)]
            unsafe {
                libc::killpg(child.id() as i32, libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
            return Err("RC watch: bridge query timeout".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    output
        .seek(SeekFrom::Start(0))
        .map_err(|_| "RC watch: output unavailable")?;
    let mut bytes = Vec::new();
    output
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "RC watch: output unavailable")?;
    if bytes.len() > 1024 * 1024 {
        return Err("RC watch: oversized bridge state".into());
    }
    let rows: Vec<Value> =
        serde_json::from_slice(&bytes).map_err(|_| "RC watch: invalid bridge state")?;
    if rows.iter().any(|r| !r.is_object()) {
        return Err("RC watch: invalid bridge row".into());
    }
    Ok(rows)
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn std::io::Write) -> Result<(), String> {
    if !args.is_empty() {
        return Err("usage: pg-spawnd rc-watch".into());
    }
    let state = crate::state::resolve_state(None, |k| std::env::var_os(k))?;
    let count = sweep(&state, &bridge_rows()?)?;
    writeln!(out, "RC watch: {count} notices").map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn consumed_notice_is_not_recreated_on_next_timer_run() {
        let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir_all(root.join("sessions")).unwrap();
        std::fs::write(
            root.join("sessions/worker.json"),
            json!({"session_id":"fixture",
            "state":"running","require_remote_control":true})
            .to_string(),
        )
        .unwrap();
        assert_eq!(sweep(&root, &[]).unwrap(), 1);
        let notices: Vec<_> = std::fs::read_dir(root.join("watchdog/outbox"))
            .unwrap()
            .collect();
        assert_eq!(notices.len(), 1);
        std::fs::remove_file(notices.into_iter().next().unwrap().unwrap().path()).unwrap();
        assert_eq!(sweep(&root, &[]).unwrap(), 0);
        assert_eq!(
            std::fs::read_dir(root.join("watchdog/outbox"))
                .unwrap()
                .count(),
            0
        );
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn disconnected_required_bridge_notifies_once_and_rearms() {
        let records = vec![json!({"name":"worker","session_id":"session-fixture",
            "state":"running","require_remote_control":true,"bridgeSessionId":"prior",
            "parent":"parent-fixture"})];
        let mut alerted = BTreeSet::new();
        let missing = vec![json!({"sessionId":"session-fixture","state":"working"})];
        let events = dropped(&records, &missing, &mut alerted);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["recipient"], "parent-fixture");
        assert!(dropped(&records, &missing, &mut alerted).is_empty());
        let connected =
            vec![json!({"sessionId":"session-fixture","state":"working","bridgeSessionId":"new"})];
        assert!(dropped(&records, &connected, &mut alerted).is_empty());
        assert_eq!(dropped(&records, &missing, &mut alerted).len(), 1);
        let optional = vec![json!({"session_id":"optional","state":"running"})];
        assert!(dropped(&optional, &[], &mut alerted).is_empty());
    }
}
