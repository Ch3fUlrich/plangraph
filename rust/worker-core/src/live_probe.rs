//! Trusted probe specifications are argv, never shell text. Evidence must come
//! from the runner's protected journal, not an item's author-supplied payload.
use serde::{Deserialize, Serialize};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const MAX_AGE: u64 = 86400;
fn timeout() -> u64 {
    30
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Spec {
    pub command: Vec<String>,
    pub expected: String,
    #[serde(default = "timeout")]
    pub timeout_secs: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResultRecord {
    pub item: String,
    pub spec: Spec,
    pub at: u64,
    pub passed: bool,
}

pub fn close_gate(
    kind: &str,
    item: &str,
    spec: Option<&Spec>,
    records: &[ResultRecord],
    now: u64,
) -> Result<(), String> {
    if kind != "feature" {
        return Ok(());
    }
    let spec = spec.ok_or("feature requires live_probe")?;
    validate(spec)?;
    // Select latest for the item, not latest passing or latest matching spec.
    let record = records
        .iter()
        .enumerate()
        .filter(|(_, r)| r.item == item)
        .max_by_key(|(i, r)| (r.at, *i))
        .map(|(_, r)| r)
        .ok_or("feature requires probe evidence")?;
    if !record.passed || record.spec != *spec || record.at > now || now - record.at > MAX_AGE {
        return Err("latest probe must pass, match current spec, and be <=24h old".into());
    }
    Ok(())
}

fn validate(spec: &Spec) -> Result<(), String> {
    if spec.command.is_empty()
        || !std::path::Path::new(&spec.command[0]).is_absolute()
        || spec.timeout_secs == 0
        || spec.timeout_secs > 300
        || spec.expected.len() > 65536
    {
        return Err("probe requires absolute executable, 1..300s timeout, <=64KiB expected".into());
    }
    Ok(())
}

pub fn run(item: &str, spec: &Spec, now: u64) -> Result<ResultRecord, String> {
    validate(spec)?;
    // File-backed output avoids pipe deadlock. Read only the expected length +1.
    let mut output = tempfile::tempfile().map_err(|e| e.to_string())?;
    let mut command = Command::new(&spec.command[0]);
    command
        .args(&spec.command[1..])
        .stdin(Stdio::null())
        .stdout(output.try_clone().map_err(|e| e.to_string())?)
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut passed = false;
    if let Ok(mut child) = command.spawn() {
        let start = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    passed = status.success();
                    break;
                }
                Ok(None) if start.elapsed() < Duration::from_secs(spec.timeout_secs) => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                _ => break,
            }
        }
        #[cfg(unix)]
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    use std::io::{Read, Seek, SeekFrom};
    output.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    output
        .take(spec.expected.len() as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    passed &= bytes == spec.expected.as_bytes();
    Ok(ResultRecord {
        item: item.into(),
        spec: spec.clone(),
        at: now,
        passed,
    })
}
