use super::{platform, Opts};
use std::{
    io,
    process::{Child, Command, ExitStatus},
    thread,
    time::Instant,
};

pub(super) struct Outcome {
    pub status: ExitStatus,
    pub stalled: bool,
    pub timed_out: bool,
    pub cancelled: bool,
}

pub(super) fn wait(
    mut child: Child,
    group: platform::Group,
    opts: &Opts,
    stalled: impl Fn() -> bool,
    mut cancelled: impl FnMut() -> io::Result<bool>,
    diagnose: impl FnOnce(u32),
) -> io::Result<Outcome> {
    let start = Instant::now();
    let mut timed_out = false;
    let mut did_stall = false;
    let mut did_cancel = false;
    let mut terminate_at = None;
    let mut diagnose = Some(diagnose);
    let result: io::Result<ExitStatus> = (|| loop {
        if let Some(status) = child.try_wait()? {
            break Ok(status);
        }
        match terminate_at {
            None => {
                timed_out = opts.timeout.is_some_and(|t| start.elapsed() >= t);
                did_stall = !timed_out && stalled();
                did_cancel = !timed_out && !did_stall && cancelled()?;
                if timed_out {
                    diagnose.take().unwrap()(child.id());
                }
                if timed_out || did_stall || did_cancel {
                    group.terminate();
                    terminate_at = Some(Instant::now());
                }
            }
            Some(t) if t.elapsed() >= opts.grace => group.kill(),
            Some(_) => {}
        }
        thread::sleep(std::time::Duration::from_millis(10));
    })();
    // Sweep descendants before dropping the group or allowing readers to join.
    #[cfg(unix)]
    let cleanup = group.finish();
    #[cfg(windows)]
    group.kill();
    #[cfg(unix)]
    cleanup?;
    if result.is_err() {
        let _ = child.wait();
    }
    let status = result?;
    Ok(Outcome {
        status,
        stalled: did_stall,
        timed_out,
        cancelled: did_cancel,
    })
}

/// Supervise a command using the caller's stdin, stdout and stderr redirections.
pub fn supervise_redirected(
    mut command: Command,
    opts: &Opts,
    cancelled: impl FnMut() -> io::Result<bool>,
) -> io::Result<ExitStatus> {
    #[cfg(unix)]
    let (group, child) = platform::Group::spawn_session(&mut command)?;
    #[cfg(windows)]
    let (group, child) = platform::Group::spawn(&mut command)?;
    drop(command);
    let result = wait(child, group, opts, || false, cancelled, |_| {})?;
    if result.timed_out {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "command timed out"));
    }
    if result.cancelled {
        return Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "command cancelled",
        ));
    }
    Ok(result.status)
}
