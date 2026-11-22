//! Launch a review finisher without keeping the caller's pipes or process group open.
use std::{
    ffi::OsStr,
    io,
    process::{Command, Stdio},
};

pub const GRACE_SECONDS: f64 = 600.0;

pub fn live(status: &str) -> bool {
    matches!(status, "queued" | "running")
}

pub fn start(args: impl IntoIterator<Item = impl AsRef<OsStr>>) -> io::Result<()> {
    let mut command = Command::new(std::env::current_exe()?);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command.spawn()?;
    Ok(())
}
