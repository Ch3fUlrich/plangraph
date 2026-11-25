//! Bounded host commands with isolated Git environment and file-backed output.
use std::{
    fs::File,
    io::{self, Read, Seek},
    process::{Command, Output, Stdio},
    time::{Duration, Instant},
};

/// Build a bounded SSH route from an ASCII host/alias or IP, optionally prefixed by a user.
pub fn ssh(destination: &str) -> io::Result<Command> {
    fn name(value: &str) -> bool {
        !value.is_empty()
            && value.len() <= 255
            && value.as_bytes()[0].is_ascii_alphanumeric()
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    }
    let hostname = match destination.split_once('@') {
        Some((user, host)) if name(user) => host,
        Some(_) => return Err(io::Error::other("invalid SSH route")),
        None => destination,
    };
    let ip = hostname
        .strip_prefix('[')
        .and_then(|s| s.strip_suffix(']'))
        .unwrap_or(hostname);
    if !name(hostname) && ip.parse::<std::net::Ipv6Addr>().is_err() {
        return Err(io::Error::other("invalid SSH route"));
    }
    let mut command = Command::new("ssh");
    command.args([
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        destination,
    ]);
    Ok(command)
}

/// A distribution name is passed through the Windows SSH default shell.
pub fn valid_wsl_distro(name: &str) -> bool {
    name.as_bytes()
        .first()
        .is_some_and(|byte| byte.is_ascii_alphanumeric() || b"._".contains(byte))
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

pub fn isolate_git(command: &mut Command) {
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command.env("GIT_TERMINAL_PROMPT", "0").stdin(Stdio::null());
}
pub fn output(command: &mut Command, timeout: Duration) -> io::Result<Vec<u8>> {
    output_up_to(command, timeout, 1024 * 1024)
}

/// Like `output`, with the largest accepted stdout given: a blob of a long log is more than 1 MiB, and the
/// deadline and the kill of the process group still apply.
pub fn output_up_to(command: &mut Command, timeout: Duration, limit: u64) -> io::Result<Vec<u8>> {
    let stdout = tempfile::tempfile()?;
    command.stdout(stdout.try_clone()?).stderr(Stdio::null());
    if !run_bounded(command, timeout)?.is_some_and(|status| status.success()) {
        return Err(io::Error::other("command failed or timed out"));
    }
    read_limited(stdout, limit)
}

/// Run one command within the supplied deadline and preserve its exit status.
pub fn capture(command: &mut Command, timeout: Duration) -> io::Result<Output> {
    let stdout = tempfile::tempfile()?;
    let stderr = tempfile::tempfile()?;
    command
        .stdout(stdout.try_clone()?)
        .stderr(stderr.try_clone()?);
    let status = run_bounded(command, timeout)?
        .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "command timed out"))?;
    Ok(Output {
        status,
        stdout: read_bounded(stdout)?,
        stderr: read_bounded(stderr)?,
    })
}

fn run_bounded(
    command: &mut Command,
    timeout: Duration,
) -> io::Result<Option<std::process::ExitStatus>> {
    isolate_git(command);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn()?;
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if start.elapsed() >= timeout {
            break None;
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    #[cfg(unix)]
    // SAFETY: this process owns the child's isolated process group.
    unsafe {
        libc::killpg(child.id() as i32, libc::SIGKILL);
    }
    if status.is_none() {
        let _ = child.kill();
    }
    let _ = child.wait();
    Ok(status)
}

fn read_bounded(file: File) -> io::Result<Vec<u8>> {
    read_limited(file, 1024 * 1024)
}

fn read_limited(mut file: File, limit: u64) -> io::Result<Vec<u8>> {
    file.rewind()?;
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(io::Error::other("command output too large"));
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn output_up_to_reads_past_the_default_cap_and_keeps_the_deadline() {
        let big = |bytes: u64| {
            let mut c = Command::new("sh");
            c.args(["-c", &format!("head -c {bytes} /dev/zero")]);
            c
        };
        assert!(output(&mut big(2 * 1024 * 1024), Duration::from_secs(20)).is_err());
        let all = output_up_to(
            &mut big(2 * 1024 * 1024),
            Duration::from_secs(20),
            4 * 1024 * 1024,
        )
        .unwrap();
        assert_eq!(all.len(), 2 * 1024 * 1024);
        assert!(output_up_to(&mut big(2048), Duration::from_secs(20), 1024).is_err());
        let mut sleeper = Command::new("sleep");
        sleeper.arg("30");
        let started = Instant::now();
        assert!(output_up_to(&mut sleeper, Duration::from_millis(200), 1024).is_err());
        assert!(started.elapsed() < Duration::from_secs(10));
    }

    #[test]
    fn ssh_routes_preserve_destination_after_option_terminator() {
        // IPv6 destinations are assembled so no address literal sits in the source.
        let gap = ":".repeat(2);
        for destination in [
            "worker-1".to_owned(),
            "worker.example.invalid".to_owned(),
            "worker_alias".to_owned(),
            "build.user@192.0.2.1".to_owned(),
            format!("user@{gap}1"),
            format!("user@[2001:db8{gap}1]"),
        ] {
            let command = ssh(&destination).unwrap();
            let args: Vec<_> = command.get_args().map(|s| s.to_str().unwrap()).collect();
            assert_eq!(
                args,
                [
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "--",
                    destination.as_str()
                ]
            );
        }
    }

    #[test]
    fn ssh_routes_reject_options_shell_characters_and_malformed_names() {
        for destination in [
            "",
            "-oProxyCommand=evil",
            "user@-worker",
            "@worker",
            "user@",
            "a@b@c",
            "worker other",
            "worker\nother",
            "worker\0other",
            "worker;evil",
            "$(evil)",
            "`evil`",
            "worker'",
            "worker\"",
            "worker\\evil",
            "worker/evil",
            "wörker",
            "[worker]",
            "worker:22",
            "user;evil@worker",
        ] {
            assert!(ssh(destination).is_err(), "accepted {destination:?}");
        }
    }
}
