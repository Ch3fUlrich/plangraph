//! Killable native-gix fetch isolation for complete-inventory refreshes.
use super::{Alarm, Mirror, Watermark};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Read, Write},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
const RESULT_LIMIT: u64 = 8 * 1024 * 1024;
#[derive(Serialize, Deserialize)]
struct Request {
    repo: String,
    remote: String,
    root: std::path::PathBuf,
}

pub(super) fn fetch(mirror: &Mirror, repo: &str, timeout: Duration) -> Result<Watermark, Alarm> {
    if timeout.is_zero() {
        return Err(Alarm::FetchTimedOut);
    }
    fs::create_dir_all(&mirror.root).map_err(|_| Alarm::FetchFailed)?;
    let mut request =
        tempfile::NamedTempFile::new_in(&mirror.root).map_err(|_| Alarm::FetchFailed)?;
    let result = tempfile::NamedTempFile::new_in(&mirror.root).map_err(|_| Alarm::FetchFailed)?;
    let payload = Request {
        repo: repo.into(),
        remote: mirror.repos[repo].clone(),
        root: mirror.root.clone(),
    };
    serde_json::to_writer(request.as_file_mut(), &payload).map_err(|_| Alarm::FetchFailed)?;
    request.flush().map_err(|_| Alarm::FetchFailed)?;
    let mut cmd = Command::new(&mirror.fetch_worker);
    cmd.arg("--fetch-one")
        .arg(request.path())
        .arg(result.path())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            cmd.env_remove(key);
        }
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let started = Instant::now();
    let mut child = cmd.spawn().map_err(|_| Alarm::FetchFailed)?;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(Alarm::FetchFailed);
            }
        }
        if started.elapsed() >= timeout {
            #[cfg(target_os = "linux")]
            unsafe {
                libc::killpg(child.id() as i32, libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
            return Err(Alarm::FetchTimedOut);
        }
        std::thread::sleep(Duration::from_millis(5).min(timeout.saturating_sub(started.elapsed())));
    };
    if !status.success() {
        return Err(Alarm::FetchFailed);
    }
    let mut bytes = Vec::new();
    fs::File::open(result.path())
        .map_err(|_| Alarm::FetchFailed)?
        .take(RESULT_LIMIT + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Alarm::FetchFailed)?;
    if bytes.len() as u64 > RESULT_LIMIT {
        return Err(Alarm::FetchFailed);
    }
    let reply: Result<Watermark, Alarm> =
        serde_json::from_slice(&bytes).map_err(|_| Alarm::FetchFailed)?;
    if reply.as_ref().is_ok_and(|w| w.repo != repo) {
        return Err(Alarm::FetchFailed);
    }
    reply
}

pub(super) fn run(request: &Path, result: &Path) -> io::Result<()> {
    let request: Request = serde_json::from_reader(fs::File::open(request)?.take(1024 * 1024))?;
    let mirror = Mirror::from_entries(
        BTreeMap::from([(request.repo.clone(), request.remote)]),
        &request.root,
    )?;
    let reply = mirror.fetch(&request.repo);
    fs::write(result, serde_json::to_vec(&reply)?)
}
