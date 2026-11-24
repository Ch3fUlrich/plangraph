//! Codex turn transport and private follow-up briefs on the writer host.
use super::Host;
use pg_worker_core::host_readiness::{bwrap_start_failure, sandbox_unavailable};
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
    let configured = command
        .get_envs()
        .find_map(|(key, value)| (key == "CARGO_TARGET_DIR").then_some(value).flatten());
    let target = pg_worker_core::cargo_cache::require_writer_target(&host.cwd, configured)
        .map_err(|error| error.to_string())?;
    // The `cargo` link on the child's PATH holds the shared lease per command, so the host can
    // reclaim the target between commands and before the push.
    command.env("CARGO_TARGET_DIR", &target);
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
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    let result = (|| {
        let status = loop {
            if host.free()? < 2 {
                return Err("disk_low: under 2 GB stop floor".into());
            }
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                break status;
            }
            std::thread::sleep(Duration::from_secs(1));
        };
        let mut log = fs::File::open(&log_path).map_err(|e| e.to_string())?;
        log.seek(SeekFrom::Start(log_start))
            .map_err(|e| e.to_string())?;
        let mut completed = false;
        let mut working_shell = false;
        let mut sandbox_error = None;
        let mut thread_id = session.map(str::to_owned);
        for line in BufReader::new(log).lines() {
            let line = line.map_err(|e| e.to_string())?;
            let Ok(event) = serde_json::from_str::<Value>(&line) else {
                if let Some(error) = bwrap_start_failure(&line) {
                    sandbox_error = Some(sandbox_unavailable(error));
                }
                continue;
            };
            let item = &event["item"];
            if event["type"] == "item.completed" && item["type"] == "command_execution" {
                if item["exit_code"].as_i64() == Some(0) {
                    working_shell = true;
                } else if let Some(error) = item["aggregated_output"]
                    .as_str()
                    .and_then(bwrap_start_failure)
                {
                    sandbox_error = Some(sandbox_unavailable(error));
                }
            }
            if event["type"] == "turn.completed" {
                completed = true;
            }
            if event["type"] == "thread.started" {
                thread_id = event["thread_id"].as_str().map(str::to_owned);
            }
        }
        if let Some(error) = sandbox_error.filter(|_| !working_shell) {
            return Err(error);
        }
        if !status.success() {
            return Err(format!("codex failed: {status}"));
        }
        if !completed {
            return Err("codex ended without a result line".into());
        }
        thread_id.ok_or_else(|| "codex did not report a session id".into())
    })();
    if result.is_err() {
        #[cfg(unix)]
        unsafe {
            libc::killpg(child.id() as i32, libc::SIGKILL);
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    #[cfg(target_os = "linux")]
    if let Ok(mut log) = fs::OpenOptions::new().append(true).open(&log_path) {
        let trim = pg_worker_core::disk_runtime::post_run_cargo(&host.cwd)
            .and_then(|report| serde_json::to_string(&report).map_err(std::io::Error::other));
        match trim {
            Ok(report) => {
                let _ = writeln!(log, "cargo post-run trim: {report}");
            }
            Err(error) => {
                let _ = writeln!(log, "cargo post-run trim skipped: {error}");
            }
        }
    }
    result
}
