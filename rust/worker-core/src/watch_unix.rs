//! Owned fresh process group. Never accepts a caller-provided pid.
use super::Child;
use crate::stall::{killpg, spawn_retrying};
use std::{
    io,
    os::unix::process::CommandExt,
    process::{Command, ExitStatus},
};

pub(super) struct Writer {
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    thread: Option<std::thread::JoinHandle<io::Result<()>>>,
}
impl Writer {
    pub(super) fn finish(mut self) -> io::Result<()> {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        self.thread
            .take()
            .unwrap()
            .join()
            .map_err(|_| io::Error::other("stdin writer panicked"))?
    }
}
impl Drop for Writer {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
pub(super) fn stdin_writer(mut pipe: std::process::ChildStdin, input: &[u8]) -> io::Result<Writer> {
    use std::{
        io::Write,
        os::fd::AsRawFd,
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        },
    };
    let fd = pipe.as_raw_fd();
    // The owned descriptor stays alive through both fcntl calls and the worker.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let stop = Arc::new(AtomicBool::new(false));
    let cancelled = stop.clone();
    let payload = input.to_vec();
    let thread = std::thread::spawn(move || {
        let mut remaining = payload.as_slice();
        while !remaining.is_empty() && !cancelled.load(Ordering::Relaxed) {
            match pipe.write(remaining) {
                Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
                Ok(n) => remaining = &remaining[n..],
                Err(e) if e.kind() == io::ErrorKind::BrokenPipe => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(10))
                }
                Err(e) => return Err(e),
            }
        }
        Ok(())
    });
    Ok(Writer {
        stop,
        thread: Some(thread),
    })
}

pub(crate) struct UnixChild {
    pub(super) child: std::process::Child,
    pgid: i32,
    gone: bool,
}
impl UnixChild {
    pub(crate) fn spawn(cmd: &mut Command) -> io::Result<Self> {
        cmd.process_group(0);
        let child = spawn_retrying(cmd)?;
        let pgid = child.id() as i32;
        Ok(Self {
            child,
            pgid,
            gone: false,
        })
    }
    fn signal(&mut self, signal: i32) -> io::Result<()> {
        if self.empty()? {
            return Ok(());
        }
        killpg(self.pgid, signal)
    }
}

#[cfg(test)]
#[path = "watch_process_tests.rs"]
mod process_tests;
impl Child for UnixChild {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.signal(libc::SIGTERM)
    }
    fn kill(&mut self) -> io::Result<()> {
        self.signal(libc::SIGKILL)
    }
    fn empty(&mut self) -> io::Result<bool> {
        // Continuous ownership survives leader reaping; never revive an absent group.
        self.gone = self.gone || group_empty(self.pgid)?;
        Ok(self.gone)
    }
    fn reap(&mut self) -> io::Result<()> {
        self.child.try_wait().map(|_| ())
    }
}

pub(super) fn group_empty(pgid: i32) -> io::Result<bool> {
    match killpg(pgid, 0) {
        Ok(()) => Ok(false),
        Err(e) if e.raw_os_error() == Some(libc::ESRCH) => Ok(true),
        Err(e) => Err(e),
    }
}
