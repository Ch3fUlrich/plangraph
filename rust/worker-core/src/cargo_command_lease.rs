//! Hold the shared Cargo lease for exactly one Cargo command of a native writer.
//!
//! The writer child finds a `cargo` shim first on its `PATH`. The shim takes the shared lease on
//! `<target>.plangraph-lock` for the command's lifetime and releases it when the command ends, so
//! the host can reclaim the target between commands. The kernel drops the lease when the shim
//! dies, whatever the cause; the Cargo child dies with it, so a cancelled command strands neither.
//! A nested Cargo command, and the preflight or fast-check commands that already hold the lease,
//! mark the environment with [`HELD_ENV`] and run without taking it again.
use crate::cargo_cache::{lease_file, writer_lease};
use std::os::fd::AsRawFd;
use std::{
    ffi::{OsStr, OsString},
    io,
    os::unix::{fs::symlink, process::CommandExt},
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};

pub use crate::cargo_cache::LEASE_HELD_ENV as HELD_ENV;
/// A reclaim runs for seconds; a Cargo command waits this long for it before it refuses.
pub const WAIT: Duration = Duration::from_secs(60);
/// Override of [`WAIT`] in milliseconds, for tests and hosts with long reclaims.
pub const WAIT_ENV: &str = "PLANGRAPH_CARGO_SHIM_WAIT_MS";
const POLL: Duration = Duration::from_millis(100);

/// True when an outer process already holds the lease for this target: the same lock file,
/// whichever spelling or symlink alias names the target.
pub fn outer_holds(target: &Path, held: Option<&OsStr>) -> bool {
    held.is_some_and(|held| {
        Path::new(held) == target || lease_file(Path::new(held)) == lease_file(target)
    })
}

/// First `cargo` on `path` that is not the shim `own` itself.
pub fn real_cargo(path: &OsStr, own: &Path) -> Option<PathBuf> {
    let own = own.canonicalize().ok();
    std::env::split_paths(path)
        .map(|dir| dir.join("cargo"))
        .filter(|candidate| candidate.is_file())
        .find(|candidate| candidate.canonicalize().ok() != own)
}

/// Make `<dir>/cargo` a link to the shim executable `exe`; idempotent.
pub fn install_shim(dir: &Path, exe: &Path) -> io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let link = dir.join("cargo");
    match std::fs::read_link(&link) {
        Ok(current) if current == exe => return Ok(link),
        Ok(_) => std::fs::remove_file(&link)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    symlink(exe, &link)?;
    Ok(link)
}

fn acquire(target: &Path, wait: Duration) -> io::Result<crate::cargo_cache::CacheLease> {
    let deadline = Instant::now() + wait;
    loop {
        match writer_lease(target) {
            Err(error)
                if error.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline =>
            {
                std::thread::sleep(POLL);
            }
            other => return other,
        }
    }
}

/// How long the shim waits for a finished Cargo command's process group to empty before it kills
/// what is left; the lease is held until the group is quiescent.
pub const GRACE: Duration = Duration::from_secs(5);
/// Override of [`GRACE`] in milliseconds, for tests and hosts with slow build teardown.
pub const GRACE_ENV: &str = "PLANGRAPH_CARGO_SHIM_GRACE_MS";

/// Run `program` with `args`; with a `target` and no outer holder it runs under the shared lease.
/// Returns the command's exit code, `128 + signal` when a signal ended it.
pub fn run_command(
    target: Option<&Path>,
    held: Option<&OsStr>,
    program: &Path,
    args: &[OsString],
    wait: Duration,
) -> io::Result<i32> {
    let grace = std::env::var(GRACE_ENV)
        .ok()
        .and_then(|ms| ms.parse().ok())
        .map_or(GRACE, Duration::from_millis);
    run_command_in_group(target, held, program, args, wait, grace)
}

/// [`run_command`] with an explicit `grace`. Under the lease Cargo runs in its own process group
/// and a guardian process holds a copy of the lease until that group is empty, so a shim that is
/// killed cannot free the lease while rustc, build scripts or a linker still run.
pub fn run_command_in_group(
    target: Option<&Path>,
    held: Option<&OsStr>,
    program: &Path,
    args: &[OsString],
    wait: Duration,
    grace: Duration,
) -> io::Result<i32> {
    let lease = match target {
        Some(target) if !outer_holds(target, held) => Some(acquire(target, wait)?),
        _ => None,
    };
    // The guardian and the group Cargo joins exist before Cargo does, so no instant remains in
    // which a killed shim could free the lease with Cargo running outside any guardian.
    let guardian = match &lease {
        Some(lease) => Some(Guardian::start(lease.file().as_raw_fd())?),
        None => None,
    };
    let group = guardian.as_ref().map(|guardian| guardian.group);
    let mut command = Command::new(program);
    command.arg0("cargo").args(args);
    if let Some(target) = target {
        command.env(HELD_ENV, target);
    }
    let shim = std::process::id() as libc::pid_t;
    // SAFETY: prctl, getppid and setpgid are async-signal-safe and the closure touches no shared
    // Rust state.
    unsafe {
        command.pre_exec(move || {
            die_with_parent(shim)?;
            if let Some(group) = group {
                if libc::setpgid(0, group) != 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }
    let spawned = command.spawn();
    if let Some(guardian) = &guardian {
        guardian.release_anchor();
    }
    let status = match spawned {
        Ok(mut child) => child.wait(),
        Err(error) => Err(error),
    };
    if let Some(guardian) = guardian {
        guardian.release(grace);
    }
    drop(lease);
    let status = status?;
    use std::os::unix::process::ExitStatusExt;
    Ok(status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)))
}

/// Close every descriptor but `a` and `b`. Async-signal-safe: raw `close_range`, with a plain
/// loop where the kernel has none.
///
/// # Safety
/// The caller owns every descriptor it does not name.
unsafe fn close_all_but(a: libc::c_int, b: libc::c_int) {
    let (low, high) = (a.min(b), a.max(b));
    let mut closed = true;
    for (first, last) in [
        (0, low - 1),
        (low + 1, high - 1),
        (high + 1, libc::c_int::MAX),
    ] {
        if first <= last && libc::syscall(libc::SYS_close_range, first as u32, last as u32, 0) != 0
        {
            closed = false;
        }
    }
    if !closed {
        for fd in 0..4096 {
            if fd != a && fd != b {
                libc::close(fd);
            }
        }
    }
}

/// Owns the process group Cargo runs in and a forked guardian that shares the lease's open file:
/// once the shim is gone (the write end of its pipe closes) the guardian kills the group and
/// exits only after the group is empty.
struct Guardian {
    /// The group Cargo joins: the id of a placeholder that keeps it alive until Cargo is in.
    group: libc::pid_t,
    anchor: libc::pid_t,
    pid: libc::pid_t,
    pipe: libc::c_int,
}

impl Guardian {
    fn start(lease: libc::c_int) -> io::Result<Self> {
        // SAFETY: the placeholder only waits for a signal.
        let anchor = match unsafe { libc::fork() } {
            -1 => return Err(io::Error::last_os_error()),
            0 => unsafe {
                libc::setpgid(0, 0);
                close_all_but(-1, -1);
                loop {
                    libc::pause();
                }
            },
            pid => pid,
        };
        // SAFETY: both processes are ours; the group exists before Cargo asks to join it.
        unsafe { libc::setpgid(anchor, anchor) };
        let mut ends = [0 as libc::c_int; 2];
        // SAFETY: `ends` is a valid two-element array.
        if unsafe { libc::pipe2(ends.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
            let error = io::Error::last_os_error();
            Self::reap(anchor);
            return Err(error);
        }
        let group = anchor;
        // SAFETY: the child runs only async-signal-safe calls (close, poll, kill, usleep, _exit).
        match unsafe { libc::fork() } {
            -1 => {
                let error = io::Error::last_os_error();
                Self::reap(anchor);
                Err(error)
            }
            0 => unsafe {
                // Keep only the lease and the shim's pipe: a copy of any other descriptor (an
                // executable another thread is still writing, a pipe a caller waits on) must
                // not outlive the shim for as long as the group runs.
                libc::close(ends[1]);
                close_all_but(lease, ends[0]);
                let mut watch = libc::pollfd {
                    fd: ends[0],
                    events: 0,
                    revents: 0,
                };
                while watch.revents & libc::POLLHUP == 0 {
                    libc::poll(&mut watch, 1, -1);
                }
                while libc::kill(-group, libc::SIGKILL) == 0 {
                    libc::usleep(10_000);
                }
                libc::_exit(0)
            },
            pid => {
                // SAFETY: the read end belongs to the guardian now.
                unsafe { libc::close(ends[0]) };
                Ok(Self {
                    group,
                    anchor,
                    pid,
                    pipe: ends[1],
                })
            }
        }
    }

    fn reap(pid: libc::pid_t) {
        // SAFETY: `pid` is our own child.
        unsafe {
            libc::kill(pid, libc::SIGKILL);
            libc::waitpid(pid, std::ptr::null_mut(), 0);
        }
    }

    /// Cargo has joined the group (or failed to start): the placeholder is no longer needed.
    fn release_anchor(&self) {
        Self::reap(self.anchor);
    }

    /// Wait for the finished command's group to empty (killing what is left after `grace`), then
    /// let the guardian go and reap it, so the lease is free the moment this returns.
    fn release(self, grace: Duration) {
        let deadline = Instant::now() + grace;
        // SAFETY: signal 0 only probes for the group.
        while unsafe { libc::kill(-self.group, 0) } == 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        // SAFETY: the pipe end and pid are this guardian's own.
        unsafe {
            libc::close(self.pipe);
            libc::waitpid(self.pid, std::ptr::null_mut(), 0);
        }
    }
}

/// Make the child die with its parent `shim`, and refuse to run Cargo when the shim already
/// exited: the lease would be released while the child survives.
fn die_with_parent(shim: libc::pid_t) -> io::Result<()> {
    // SAFETY: prctl and getppid take no pointers and have no preconditions.
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::getppid() } != shim {
        return Err(io::Error::other(
            "the cargo shim exited before the command started",
        ));
    }
    Ok(())
}

/// Entry point of the shim: `args` are the arguments the writer gave `cargo`.
pub fn shim_main(args: impl Iterator<Item = OsString>) -> i32 {
    let own = std::env::current_exe().unwrap_or_default();
    let path = std::env::var_os("PATH").unwrap_or_default();
    let Some(real) = real_cargo(&path, &own) else {
        eprintln!("cargo shim: no cargo found on PATH");
        return 127;
    };
    let target = std::env::var_os("CARGO_TARGET_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    let args: Vec<OsString> = args.collect();
    match run_command(
        target.as_deref(),
        std::env::var_os(HELD_ENV).as_deref(),
        &real,
        &args,
        std::env::var(WAIT_ENV)
            .ok()
            .and_then(|ms| ms.parse().ok())
            .map_or(WAIT, Duration::from_millis),
    ) {
        Ok(code) => code,
        Err(error) => {
            // A refused lease already names its holders (`writer_lease`).
            eprintln!("cargo shim: {error}");
            1
        }
    }
}

#[cfg(test)]
#[path = "cargo_command_lease_tests.rs"]
mod tests;
