//! Exact-PID and PID-file process check/wait helper; never scans process argvs.
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const USAGE: &str = "usage: plangraph wait-pid (--pid PID | --pid-file PATH) [--expect SUBSTR] [--check | --timeout SECS] [--poll-ms MS]";
const DEFAULT_POLL: Duration = Duration::from_millis(50);

#[derive(Debug, PartialEq)]
pub enum Target {
    Pid(u32),
    PidFile(PathBuf),
}

#[derive(Debug, PartialEq)]
struct Options {
    target: Target,
    expect: Option<String>,
    check_only: bool,
    timeout: Option<Duration>,
    poll: Duration,
}

fn parse_args(args: &[String]) -> Result<Options, String> {
    let mut pid: Option<u32> = None;
    let mut pid_file: Option<PathBuf> = None;
    let mut expect: Option<String> = None;
    let mut check_only = false;
    let mut timeout: Option<Duration> = None;
    let mut poll = DEFAULT_POLL;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--pid" if pid.is_none() && pid_file.is_none() => {
                let raw = args.get(i + 1).ok_or_else(|| USAGE.to_string())?;
                let parsed = raw
                    .parse::<u32>()
                    .ok()
                    .filter(|&n| n > 0)
                    .ok_or_else(|| format!("invalid --pid: {raw}"))?;
                pid = Some(parsed);
                i += 2;
            }
            "--pid-file" if pid.is_none() && pid_file.is_none() => {
                let raw = args.get(i + 1).ok_or_else(|| USAGE.to_string())?;
                if raw.is_empty() {
                    return Err("empty --pid-file".into());
                }
                pid_file = Some(PathBuf::from(raw));
                i += 2;
            }
            "--expect" if expect.is_none() => {
                let raw = args.get(i + 1).ok_or_else(|| USAGE.to_string())?;
                if raw.is_empty() {
                    return Err("empty --expect".into());
                }
                expect = Some(raw.clone());
                i += 2;
            }
            "--check" if !check_only && timeout.is_none() => {
                check_only = true;
                i += 1;
            }
            "--timeout" if timeout.is_none() && !check_only => {
                let raw = args.get(i + 1).ok_or_else(|| USAGE.to_string())?;
                let secs = raw
                    .parse::<f64>()
                    .ok()
                    .filter(|s| s.is_finite() && *s >= 0.0)
                    .ok_or_else(|| format!("invalid --timeout: {raw}"))?;
                timeout = Some(Duration::from_secs_f64(secs));
                i += 2;
            }
            "--poll-ms" => {
                let raw = args.get(i + 1).ok_or_else(|| USAGE.to_string())?;
                let ms = raw
                    .parse::<u64>()
                    .ok()
                    .filter(|&m| m > 0)
                    .ok_or_else(|| format!("invalid --poll-ms: {raw}"))?;
                poll = Duration::from_millis(ms);
                i += 2;
            }
            _ => return Err(USAGE.into()),
        }
    }
    let target = match (pid, pid_file) {
        (Some(p), None) => Target::Pid(p),
        (None, Some(path)) => Target::PidFile(path),
        _ => return Err(USAGE.into()),
    };
    Ok(Options {
        target,
        expect,
        check_only,
        timeout,
        poll,
    })
}

fn read_pid_file(path: &Path) -> io::Result<Option<u32>> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(text.trim().parse::<u32>().ok().filter(|&n| n > 0)),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(err),
    }
}

#[cfg(unix)]
fn is_zombie_stat(stat: &str) -> bool {
    stat.rsplit_once(')')
        .and_then(|(_, rest)| rest.split_whitespace().next())
        .is_some_and(|state| matches!(state, "Z" | "X"))
}

#[cfg(unix)]
fn proc_cmdline_matches(pid: u32, expect: &str) -> io::Result<bool> {
    let path = PathBuf::from(format!("/proc/{pid}/cmdline"));
    match fs::read(&path) {
        Ok(bytes) => {
            let cmd = String::from_utf8_lossy(&bytes).replace('\0', " ");
            Ok(cmd.contains(expect))
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => {
            // Treat missing /proc/<pid>/cmdline as exited only when procfs is mounted.
            Ok(!Path::new("/proc").is_dir())
        }
        Err(err) if err.kind() == io::ErrorKind::PermissionDenied => Ok(true),
        Err(err) => Err(err),
    }
}

#[cfg(unix)]
pub fn is_pid_alive(pid: u32, expect: Option<&str>) -> io::Result<bool> {
    if pid == 0 || pid == std::process::id() {
        return Ok(false);
    }
    let Ok(raw_pid) = libc::pid_t::try_from(pid) else {
        return Ok(false);
    };
    if let Ok(stat) = fs::read_to_string(format!("/proc/{pid}/stat")) {
        if is_zombie_stat(&stat) {
            return Ok(false);
        }
    }
    // SAFETY: signal 0 queries process existence without delivering a signal.
    let rc = unsafe { libc::kill(raw_pid, 0) };
    if rc != 0 {
        let err = io::Error::last_os_error();
        return match err.raw_os_error() {
            Some(libc::ESRCH) => Ok(false),
            Some(libc::EPERM) => match expect {
                Some(substr) => proc_cmdline_matches(pid, substr),
                None => Ok(true),
            },
            _ => Err(err),
        };
    }
    match expect {
        Some(substr) => proc_cmdline_matches(pid, substr),
        None => Ok(true),
    }
}

#[cfg(windows)]
pub fn is_pid_alive(pid: u32, _expect: Option<&str>) -> io::Result<bool> {
    use windows_sys::Win32::Foundation::{
        CloseHandle, ERROR_INVALID_PARAMETER, HANDLE, STILL_ACTIVE, WAIT_OBJECT_0,
    };
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
        SYNCHRONIZE,
    };
    if pid == 0 || pid == std::process::id() {
        return Ok(false);
    }
    // SAFETY: handle is checked for null and closed before returning.
    let handle: HANDLE =
        unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid) };
    if handle.is_null() {
        let err = io::Error::last_os_error();
        if err.raw_os_error() == Some(ERROR_INVALID_PARAMETER as i32) {
            return Ok(false);
        }
        return Err(err);
    }
    let wait = unsafe { WaitForSingleObject(handle, 0) };
    let mut code: u32 = 0;
    let ok = unsafe { GetExitCodeProcess(handle, &mut code) };
    unsafe { CloseHandle(handle) };
    if wait == WAIT_OBJECT_0 {
        return Ok(false);
    }
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(code as i32 == STILL_ACTIVE)
}

fn resolve_pid(target: &Target) -> io::Result<Option<u32>> {
    match target {
        Target::Pid(pid) => Ok(Some(*pid)),
        Target::PidFile(path) => read_pid_file(path),
    }
}

/// Return `true` if the resolved exact PID is alive (and matches `expect` on Unix).
pub fn check_target(target: &Target, expect: Option<&str>) -> io::Result<bool> {
    match resolve_pid(target)? {
        Some(pid) => is_pid_alive(pid, expect),
        None => Ok(false),
    }
}

/// Wait until the resolved exact PID exits; returns `true` when gone, `false` on timeout.
pub fn wait_target(
    target: &Target,
    expect: Option<&str>,
    timeout: Option<Duration>,
    poll: Duration,
) -> io::Result<bool> {
    let Some(pid) = resolve_pid(target)? else {
        return Ok(true);
    };
    let start = Instant::now();
    while is_pid_alive(pid, expect)? {
        if let Some(limit) = timeout {
            if start.elapsed() >= limit {
                return Ok(false);
            }
        }
        std::thread::sleep(poll);
    }
    Ok(true)
}

pub fn cli(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("wait-pid") {
        return None;
    }
    let rest = &args[1..];
    if rest.first().is_some_and(|a| a == "--help" || a == "-h") && rest.len() == 1 {
        println!("{USAGE}");
        return Some(0);
    }
    let opts = match parse_args(rest) {
        Ok(opts) => opts,
        Err(err) => {
            eprintln!("{err}");
            return Some(2);
        }
    };
    let result = if opts.check_only {
        check_target(&opts.target, opts.expect.as_deref())
    } else {
        wait_target(
            &opts.target,
            opts.expect.as_deref(),
            opts.timeout,
            opts.poll,
        )
    };
    Some(match result {
        Ok(true) => 0,
        Ok(false) => 1,
        Err(err) => {
            eprintln!("wait-pid: {err}");
            2
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_args_validates_target_and_exclusive_flags() {
        assert!(parse_args(&[]).is_err());
        assert!(parse_args(&["--pid".into(), "0".into()]).is_err());
        assert!(
            parse_args(&["--pid".into(), "1".into(), "--pid-file".into(), "a".into()]).is_err()
        );
        assert!(parse_args(&[
            "--pid".into(),
            "1".into(),
            "--check".into(),
            "--timeout".into(),
            "1".into()
        ])
        .is_err());
        assert!(
            parse_args(&["--pid".into(), "1".into(), "--timeout".into(), "-1".into()]).is_err()
        );
        assert!(parse_args(&["--pid".into(), "1".into(), "--poll-ms".into(), "0".into()]).is_err());

        let parsed = parse_args(&[
            "--pid-file".into(),
            "run.pid".into(),
            "--expect".into(),
            "codex exec -m".into(),
            "--timeout".into(),
            "2.5".into(),
            "--poll-ms".into(),
            "20".into(),
        ])
        .unwrap();
        assert_eq!(parsed.target, Target::PidFile("run.pid".into()));
        assert_eq!(parsed.expect.as_deref(), Some("codex exec -m"));
        assert_eq!(parsed.timeout, Some(Duration::from_secs_f64(2.5)));
        assert_eq!(parsed.poll, Duration::from_millis(20));
    }

    #[test]
    fn read_pid_file_handles_missing_invalid_and_valid_files() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("worker.pid");
        assert_eq!(read_pid_file(&path).unwrap(), None);
        fs::write(&path, "not-a-pid\n").unwrap();
        assert_eq!(read_pid_file(&path).unwrap(), None);
        fs::write(&path, "0\n").unwrap();
        assert_eq!(read_pid_file(&path).unwrap(), None);
        fs::write(&path, "4242\n").unwrap();
        assert_eq!(read_pid_file(&path).unwrap(), Some(4242));
    }

    #[cfg(unix)]
    #[test]
    fn zombie_stat_state_is_detected() {
        assert!(is_zombie_stat("123 (bash -c) Z 1 123 123 0 -1 0"));
        assert!(is_zombie_stat("123 (a)b) X 1 123"));
        assert!(!is_zombie_stat("123 (codex) S 1 123"));
    }
}
