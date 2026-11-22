//! Codex turn transport and private follow-up briefs on the writer host.
use super::Host;
use serde_json::Value;
use std::{
    fs,
    io::{BufRead, BufReader, Seek, SeekFrom, Write},
    path::Path,
    process::Stdio,
    time::Duration,
};

fn follow_up_payload(
    host: &Host,
    directory: &Path,
    session: Option<&str>,
    prompt: &str,
) -> Result<String, String> {
    let turn = format!("{prompt}\nReview directory: {}\n", host.reviews.display());
    if session.is_none() {
        return Ok(turn);
    }
    let mut file = tempfile::Builder::new()
        .prefix("follow-up-")
        .suffix(".md")
        .tempfile_in(directory)
        .map_err(|e| format!("follow-up brief: {e}"))?;
    write!(file, "{}\n\n# Follow-up\n\n{turn}", host.brief)
        .and_then(|()| file.as_file().sync_all())
        .map_err(|e| format!("follow-up brief: {e}"))?;
    let path = file
        .into_temp_path()
        .keep()
        .map_err(|e| format!("follow-up brief: {e}"))?;
    let path = serde_json::to_string(&path).map_err(|e| format!("follow-up brief path: {e}"))?;
    Ok(format!("Read follow-up brief at {path}\n"))
}

pub(super) fn execute(host: &Host, session: Option<&str>, prompt: &str) -> Result<String, String> {
    let _guard = crate::writer_control::mutation_guard(&host.record_path)?;
    // The caller PATH recorded at admission selects Codex for every launch and resume.
    let codex = crate::native_writer_tool::resolve(host.tool_path.as_ref(), &host.cwd)?;
    let directory = host
        .record_path
        .parent()
        .ok_or("record directory missing")?;
    let log_path = directory.join("native.log");
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| e.to_string())?;
    let log_start = log.metadata().map_err(|e| e.to_string())?.len();
    let mut command = host.writer_command(codex, session, directory)?;
    command
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log);
    let payload = follow_up_payload(host, directory, session, prompt)?;
    let mut input = tempfile::tempfile_in(directory).map_err(|e| e.to_string())?;
    input
        .write_all(payload.as_bytes())
        .map_err(|e| e.to_string())?;
    input.rewind().map_err(|e| e.to_string())?;
    command.stdin(Stdio::from(input));
    let opts = pg_worker_core::stall::Opts {
        timeout: Some(host.turn_timeout),
        grace: Duration::from_secs(1),
        ..Default::default()
    };
    let mut disk_check = None;
    let status = pg_worker_core::stall::supervise_redirected(command, &opts, || {
        if disk_check
            .is_none_or(|last: std::time::Instant| last.elapsed() >= Duration::from_secs(1))
        {
            disk_check = Some(std::time::Instant::now());
            if host.free().map_err(std::io::Error::other)? < 2 {
                return Err(std::io::Error::other("disk_low: under 2 GB stop floor"));
            }
        }
        Ok(host.cancelled())
    })
    .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(format!("codex failed: {status}"));
    }
    let mut log = fs::File::open(&log_path).map_err(|e| e.to_string())?;
    log.seek(SeekFrom::Start(log_start))
        .map_err(|e| e.to_string())?;
    let mut completed = false;
    let mut thread_id = session.map(str::to_owned);
    for line in BufReader::new(log).lines() {
        let line = line.map_err(|e| e.to_string())?;
        let Ok(event) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if event["type"] == "turn.completed" {
            completed = true;
        }
        if event["type"] == "thread.started" {
            thread_id = event["thread_id"].as_str().map(str::to_owned);
        }
    }
    if !completed {
        return Err("codex ended without a result line".into());
    }
    thread_id.ok_or_else(|| "codex did not report a session id".into())
}
