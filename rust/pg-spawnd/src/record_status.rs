//! Status readers reconcile snapshots; only watchdogs persist state changes.
use serde_json::{json, Value};
use std::{
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

pub(crate) fn prompt_only_missing(record: &Value, directory: &Path) -> bool {
    let leg = record
        .get("chosen_leg")
        .filter(|l| l.is_object())
        .unwrap_or(&record["plan"]["first_leg"]);
    let mode = leg["tool_mode"]
        .as_str()
        .or_else(|| record["tool_mode"].as_str());
    if record["role"] != "review" || mode != Some("prompt-only") {
        return false;
    }
    if matches!(record["status"].as_str(), Some("running" | "queued")) && record["ended"].is_null()
    {
        return false;
    }
    use std::io::Read;
    let mut text = String::new();
    let Ok(file) = std::fs::File::open(directory.join("prompt.txt")) else {
        return true;
    };
    if file
        .take(8 * 1024 * 1024 + 1)
        .read_to_string(&mut text)
        .is_err()
        || text.len() > 8 * 1024 * 1024
    {
        return true;
    }
    let mut header = false;
    for line in text.lines() {
        if line.starts_with("diff --git ") {
            header = true;
        } else if header
            && (line.starts_with("--- ")
                || line.starts_with("+++ ")
                || line.starts_with("@@ ")
                || line.starts_with("GIT binary patch")
                || line.starts_with("Binary files ")
                || line.starts_with("old mode ")
                || line.starts_with("new file mode ")
                || line.starts_with("deleted file mode "))
        {
            return false;
        }
    }
    true
}

fn reconcile_with(
    record: &mut Value,
    directory: &Path,
    now: f64,
    live: impl FnOnce(u32, Option<u64>) -> Option<bool>,
) {
    if record["status"] != "running" || record["overflow_remote"] == true {
        return;
    }
    if let Ok(modified) = directory
        .join("live.log")
        .metadata()
        .and_then(|m| m.modified())
    {
        if let Ok(stamp) = modified.duration_since(UNIX_EPOCH) {
            record["live_log_age_s"] = json!((now - stamp.as_secs_f64()).max(0.0));
        }
    }
    let Some(pid) = record["pid"]
        .as_u64()
        .and_then(|p| u32::try_from(p).ok())
        .filter(|p| *p > 0)
    else {
        return;
    };
    if live(pid, record["pid_start"].as_u64()) == Some(false) {
        record["status"] = json!("exited");
        record["reason"] = json!("pid_gone");
        record["ended"] = json!(now);
    }
}

pub(crate) fn reconcile(record: &mut Value, directory: &Path) {
    if record["pid"].as_u64().is_some_and(|p| p > 0) && record["launched_leg"].is_null() {
        let leg = record
            .get("chosen_leg")
            .filter(|l| l.is_object())
            .unwrap_or(&record["plan"]["first_leg"]);
        if leg.is_object() {
            record["launched_leg"] = leg.clone();
        }
    }
    if prompt_only_missing(record, directory) {
        record["verdict_status"] = json!("void");
        record["verdict_void_reason"] = json!("prompt_only_no_diff");
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    reconcile_with(record, directory, now, process_live);
}

#[cfg(target_os = "linux")]
fn process_live(pid: u32, start: Option<u64>) -> Option<bool> {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => {
            let (_, rest) = stat.rsplit_once(')')?;
            let fields: Vec<_> = rest.split_whitespace().collect();
            let token = fields.get(19)?.parse::<u64>().ok()?;
            Some(!matches!(*fields.first()?, "Z" | "X") && start.is_none_or(|s| s == token))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Some(false),
        Err(_) => None,
    }
}
#[cfg(all(unix, not(target_os = "linux")))]
fn process_live(pid: u32, _start: Option<u64>) -> Option<bool> {
    if unsafe { libc::kill(pid as i32, 0) } == 0 {
        return Some(true);
    }
    (std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)).then_some(false)
}
#[cfg(windows)]
fn process_live(pid: u32, start: Option<u64>) -> Option<bool> {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError, FILETIME},
        System::Threading::{
            GetExitCodeProcess, GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        },
    };
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return (GetLastError() == 87).then_some(false);
        }
        let mut code = 0;
        let mut times: [FILETIME; 4] = std::mem::zeroed();
        let known = GetExitCodeProcess(handle, &mut code) != 0;
        let token = if GetProcessTimes(
            handle,
            &mut times[0],
            &mut times[1],
            &mut times[2],
            &mut times[3],
        ) != 0
        {
            Some((u64::from(times[0].dwHighDateTime) << 32) | u64::from(times[0].dwLowDateTime))
        } else {
            None
        };
        CloseHandle(handle);
        if !known {
            None
        } else if code != 259 {
            Some(false)
        } else {
            token.map(|token| start.is_none_or(|s| s == token))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prompt_only_records_without_patch_are_void() {
        let root = tempfile::tempdir().unwrap();
        let record =
            json!({"status":"completed","role":"review","chosen_leg":{"tool_mode":"prompt-only"}});
        let mut missing = record.clone();
        reconcile(&mut missing, root.path());
        assert_eq!(missing["verdict_status"], "void");
        std::fs::write(
            root.path().join("prompt.txt"),
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        )
        .unwrap();
        assert!(!prompt_only_missing(&record, root.path()));
    }
    #[test]
    fn remote_overflow_process_is_not_queried_locally() {
        let root = tempfile::tempdir().unwrap();
        let mut record = json!({"status":"running","pid":42,"overflow_remote":true});
        reconcile_with(&mut record, root.path(), 100.0, |_, _| {
            panic!("remote PID queried locally")
        });
        assert_eq!(record["status"], "running");
    }
    #[test]
    fn dead_pid_exits_on_next_read_without_inventing_exit_code() {
        let root = tempfile::tempdir().unwrap();
        for live in [Some(false), Some(true), None] {
            let mut run = json!({"status":"running","pid":42,"pid_start":123,"exit_code":null});
            reconcile_with(&mut run, root.path(), 100.0, |pid, start| {
                assert_eq!((pid, start), (42, Some(123)));
                live
            });
            assert_eq!(
                run["status"],
                if live == Some(false) {
                    "exited"
                } else {
                    "running"
                }
            );
            if live == Some(false) {
                assert_eq!(run["reason"], "pid_gone");
            }
            assert!(run["exit_code"].is_null());
        }
    }
}
