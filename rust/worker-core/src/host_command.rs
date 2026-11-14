//! Bounded host commands with isolated Git environment and file-backed output.
use std::{
    fs::File,
    io::{self, Read, Seek},
    process::{Command, Stdio},
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

pub fn isolate_git(command: &mut Command) {
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command.env("GIT_TERMINAL_PROMPT", "0").stdin(Stdio::null());
}
pub fn output(command: &mut Command, timeout: Duration) -> io::Result<Vec<u8>> {
    let mut file: File = tempfile::tempfile()?;
    isolate_git(command);
    command.stdout(file.try_clone()?).stderr(Stdio::null());
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
    if !status.is_some_and(|s| s.success()) {
        return Err(io::Error::other("command failed or timed out"));
    }
    file.rewind()?;
    let mut bytes = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err(io::Error::other("command output too large"));
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ssh_routes_preserve_destination_after_option_terminator() {
        for destination in [
            "worker-1",
            "worker.example.invalid",
            "worker_alias",
            "build.user@192.0.2.1",
            "user@::1",
            "user@[2001:db8::1]",
        ] {
            let command = ssh(destination).unwrap();
            let args: Vec<_> = command.get_args().map(|s| s.to_str().unwrap()).collect();
            assert_eq!(
                args,
                [
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "--",
                    destination
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
            assert!(crate::fleet_collect::remote("worker", destination).is_err());
        }
    }
}
