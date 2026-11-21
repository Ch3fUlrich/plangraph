//! Codex turn transport and private follow-up briefs on the writer host.
use super::{model_args, Host};
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
    // The caller PATH recorded at admission selects Codex for every launch and resume.
    let codex = crate::native_writer_tool::resolve(host.tool_path.as_ref(), &host.cwd)?;
    let directory = host
        .record_path
        .parent()
        .ok_or("record directory missing")?;
    let payload = follow_up_payload(host, directory, session, prompt)?;
    let log_path = directory.join("native.log");
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| e.to_string())?;
    let log_start = log.metadata().map_err(|e| e.to_string())?.len();
    let mut command = host.command(codex);
    command.arg("exec");
    if let Some(id) = session {
        command.args(["resume", id]);
    }
    command
        .env(
            "CARGO_TARGET_DIR",
            pg_worker_core::cargo_cache::shared_cache_roots()
                .map_err(|e| e.to_string())?
                .remove(0),
        )
        .env("CARGO_INCREMENTAL", "0")
        .args(["--json"])
        .args(model_args(&host.effort))
        .args([
            "-c",
            "approval_policy=never",
            "-c",
            &format!(
                "sandbox_mode=\"{}\"",
                pg_worker_core::host_readiness::WRITER_SANDBOX
            ),
            "-o",
        ])
        .arg(directory.join("native.last"))
        .arg("-")
        .current_dir(&host.cwd)
        .stdin(Stdio::piped())
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    let mut stdin = child.stdin.take().ok_or("writer stdin missing")?;
    // A reader failure or disk probe failure must terminate the child too.
    let result = (|| {
        stdin
            .write_all(payload.as_bytes())
            .map_err(|e| e.to_string())?;
        drop(stdin);
        loop {
            if host.free()? < 2 {
                return Err("disk_low: under 2 GB stop floor".into());
            }
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                if !status.success() {
                    return Err(format!("codex failed: {status}"));
                }
                break;
            }
            std::thread::sleep(Duration::from_secs(1));
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
    })();
    if result.is_err() {
        #[cfg(unix)]
        unsafe {
            libc::killpg(child.id() as i32, libc::SIGKILL);
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}
