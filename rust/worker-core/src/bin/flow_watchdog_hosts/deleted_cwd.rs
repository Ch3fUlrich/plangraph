use super::Alert;
use std::{fs, io, os::unix::ffi::OsStrExt, path::Path};

const MIN_AGE_SECONDS: f64 = 2.0 * 60.0 * 60.0;

pub(crate) fn alert(proc: &Path) -> io::Result<Option<Alert>> {
    let uptime = fs::read_to_string(proc.join("uptime"))?
        .split_whitespace()
        .next()
        .and_then(|time| time.parse::<f64>().ok())
        .filter(|time| time.is_finite() && *time >= 0.0)
        .ok_or_else(|| io::Error::other("invalid process uptime"))?;
    let ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if ticks <= 0 {
        return Err(io::Error::last_os_error());
    }
    let oldest = (uptime - MIN_AGE_SECONDS) * ticks as f64;
    let count = fs::read_dir(proc)?
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().as_bytes().iter().all(u8::is_ascii_digit))
        .map(|entry| entry.path())
        .filter(|dir| {
            use pg_worker_core::run::group_unix::parse_stat;
            fs::read_to_string(dir.join("stat"))
                .ok()
                .and_then(|stat| parse_stat(&stat))
                .is_some_and(|(state, _, start)| {
                    state != 'Z' && state != 'X' && start as f64 <= oldest
                })
                && fs::read_link(dir.join("cwd"))
                    .is_ok_and(|cwd| cwd.as_os_str().as_bytes().ends_with(b" (deleted)"))
        })
        .count();
    if count == 0 {
        return Ok(None);
    }
    let hostname = fs::read_to_string(proc.join("sys/kernel/hostname"))?;
    let host = hostname.trim();
    Ok(Some(Alert {
        active: format!("deleted-cwd {host}"),
        message: format!("deleted-cwd {host} {count} processes"),
    }))
}
