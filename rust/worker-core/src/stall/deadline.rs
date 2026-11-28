use super::platform::Group;
use std::{
    io,
    process::{Command, ExitStatus},
    thread,
    time::{Duration, Instant},
};

/// Run with caller-supplied stdio and reap the child before returning.
pub(crate) fn status_until(command: &mut Command, deadline: Instant) -> io::Result<ExitStatus> {
    status_until_with(command, deadline, Instant::now, thread::sleep)
}

/// Run and reap a child using caller-supplied observations of the deadline.
pub fn status_until_with(
    command: &mut Command,
    deadline: Instant,
    now: impl Fn() -> Instant,
    mut sleep: impl FnMut(Duration),
) -> io::Result<ExitStatus> {
    let timeout = || io::Error::new(io::ErrorKind::TimedOut, "child deadline expired");
    if now() >= deadline {
        return Err(timeout());
    }
    let (group, mut child) = Group::spawn(command)?;
    let result = loop {
        let remaining = deadline.saturating_duration_since(now());
        if remaining.is_zero() {
            break Err(timeout());
        }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => sleep(remaining.min(Duration::from_millis(5))),
            Err(error) => break Err(error),
        }
    };
    group.kill();
    if result.is_err() {
        let _ = child.kill();
        child.wait()?;
    }
    result
}
