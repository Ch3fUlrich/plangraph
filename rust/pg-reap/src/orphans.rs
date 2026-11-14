//! Linux orphan reaping from explicit, previously recorded ownership evidence.
use serde::Deserialize;
use std::{
    fs, io,
    path::{Path, PathBuf},
};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub pid: u32,
    pub start_ticks: u64,
    pub cwd: PathBuf,
}

/// A PID alone, a matching directory alone, or a living parent is never proof.
pub fn inspect(proc_root: &Path, evidence: &Evidence) -> io::Result<bool> {
    if evidence.pid <= 1 || !evidence.cwd.is_absolute() || evidence.start_ticks == 0 {
        return Ok(false);
    }
    let root = proc_root.join(evidence.pid.to_string());
    let stat = match fs::read_to_string(root.join("stat")) {
        Ok(stat) => stat,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e),
    };
    let Some((_, fields)) = stat.rsplit_once(')') else {
        return Ok(false);
    };
    let fields: Vec<_> = fields.split_whitespace().collect();
    Ok(fields.first().is_some_and(|s| *s != "Z")
        && fields.get(1) == Some(&"1")
        && fields.get(19).and_then(|s| s.parse::<u64>().ok()) == Some(evidence.start_ticks)
        && fs::read_link(root.join("cwd"))? == evidence.cwd)
}

#[cfg(target_os = "linux")]
pub fn reap(evidence: &Evidence) -> io::Result<bool> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    // Open first, then verify: PID reuse after the check cannot redirect the signal.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, evidence.pid, 0) } as i32;
    if fd < 0 {
        let error = io::Error::last_os_error();
        return if error.raw_os_error() == Some(libc::ESRCH) {
            Ok(false)
        } else {
            Err(error)
        };
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    if !inspect(Path::new("/proc"), evidence)? {
        return Ok(false);
    }
    if unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd.as_raw_fd(),
            libc::SIGKILL,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    let mut poll = libc::pollfd {
        fd: fd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    if unsafe { libc::poll(&mut poll, 1, 5000) } <= 0 {
        return Err(io::Error::other("kill_incomplete"));
    }
    Ok(true)
}
