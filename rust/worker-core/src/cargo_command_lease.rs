//! Hold the shared Cargo lease for exactly one Cargo command of a native writer.
//!
//! The writer child finds a `cargo` shim first on its `PATH`. The shim takes the shared lease on
//! `<target>.plangraph-lock` for the command's lifetime and releases it when the command ends, so
//! the host can reclaim the target between commands. The kernel drops the lease when the shim
//! dies, whatever the cause; the Cargo child dies with it, so a cancelled command strands neither.
//! A nested Cargo command, and the preflight or fast-check commands that already hold the lease,
//! mark the environment with [`HELD_ENV`] and run without taking it again.
use crate::cargo_cache::{lease_file, writer_lease};
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

/// Run `program` with `args`; with a `target` and no outer holder it runs under the shared lease.
/// Returns the command's exit code, `128 + signal` when a signal ended it.
pub fn run_command(
    target: Option<&Path>,
    held: Option<&OsStr>,
    program: &Path,
    args: &[OsString],
    wait: Duration,
) -> io::Result<i32> {
    let lease = match target {
        Some(target) if !outer_holds(target, held) => Some(acquire(target, wait)?),
        _ => None,
    };
    let mut command = Command::new(program);
    command.arg0("cargo").args(args);
    if let Some(target) = target {
        command.env(HELD_ENV, target);
    }
    let shim = std::process::id() as libc::pid_t;
    // SAFETY: prctl and getppid are async-signal-safe and the closure touches no shared Rust state.
    unsafe {
        command.pre_exec(move || die_with_parent(shim));
    }
    let status = command.status();
    drop(lease);
    let status = status?;
    use std::os::unix::process::ExitStatusExt;
    Ok(status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)))
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
