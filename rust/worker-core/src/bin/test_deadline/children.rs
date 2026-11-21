//! Adopt detached test descendants in the dedicated, single-case-at-a-time runner.
use std::io;
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

pub(super) fn adopt() -> io::Result<()> {
    #[cfg(target_os = "linux")]
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub(super) fn reap() -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            // The direct test and the debugger have already been waited for.
            match unsafe { libc::waitpid(-1, std::ptr::null_mut(), libc::WNOHANG) } {
                0 => {}
                -1 => {
                    let error = io::Error::last_os_error();
                    match error.raw_os_error() {
                        Some(libc::ECHILD) => return Ok(()),
                        Some(libc::EINTR) => continue,
                        _ => return Err(error),
                    }
                }
                _ => continue,
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "detached test descendants did not exit",
                ));
            }
            kill_adopted()?;
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    #[cfg(not(target_os = "linux"))]
    Ok(())
}

#[cfg(target_os = "linux")]
fn kill_adopted() -> io::Result<()> {
    for task in std::fs::read_dir("/proc/self/task")? {
        let file = task?.path().join("children");
        let pids = match std::fs::read_to_string(file) {
            Ok(pids) => pids,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        for pid in pids.split_whitespace() {
            let pid = pid.parse::<libc::pid_t>().map_err(io::Error::other)?;
            // A direct child cannot have its PID recycled until this runner reaps it.
            if pid > 1 && unsafe { libc::kill(pid, libc::SIGKILL) } < 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(error);
                }
            }
        }
    }
    Ok(())
}
