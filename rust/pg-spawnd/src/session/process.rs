use std::{
    io::{self, Read, Seek, Write},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

pub(super) fn capture(
    command: &mut Command,
    input: Option<&[u8]>,
    timeout: Duration,
) -> io::Result<Output> {
    // The SSH exchange carries both input and output and inherits the caller's
    // environment. File-backed stdio bounds the wait even if a peer holds a descriptor.
    let mut stdout = tempfile::tempfile()?;
    let mut stderr = tempfile::tempfile()?;
    let stdin = if let Some(bytes) = input {
        let mut stdin = tempfile::tempfile()?;
        stdin.write_all(bytes)?;
        stdin.rewind()?;
        Stdio::from(stdin)
    } else {
        Stdio::inherit()
    };
    let mut child = command
        .stdin(stdin)
        .stdout(stdout.try_clone()?)
        .stderr(stderr.try_clone()?)
        .spawn()?;
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        let remaining = timeout.saturating_sub(start.elapsed());
        if remaining.is_zero() {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(io::ErrorKind::TimedOut, "command timed out"));
        }
        thread::sleep(remaining.min(Duration::from_millis(10)));
    };
    stdout.rewind()?;
    stderr.rewind()?;
    let (mut out, mut err) = (Vec::new(), Vec::new());
    stdout.read_to_end(&mut out)?;
    stderr.read_to_end(&mut err)?;
    Ok(Output {
        status,
        stdout: out,
        stderr: err,
    })
}
