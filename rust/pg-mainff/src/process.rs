use std::{
    io::Read,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Missing,
    Unavailable,
    Timeout,
}
#[derive(Debug, Clone)]
pub struct Output {
    pub code: i32,
    pub out: String,
    pub err: String,
}
pub trait Runner {
    fn run(&self, args: &[&str]) -> Result<Output, Error>;
}
pub struct System;
impl Runner for System {
    fn run(&self, args: &[&str]) -> Result<Output, Error> {
        let mut cmd = Command::new(args[0]);
        cmd.args(&args[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // Git hooks inherit bindings to the caller's checkout. Explicit -C wins.
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("GIT_") {
                cmd.env_remove(key);
            }
        }
        cmd.env("GIT_TERMINAL_PROMPT", "0");
        let mut child = cmd.spawn().map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                Error::Missing
            } else {
                Error::Unavailable
            }
        })?;
        fn drain(
            mut pipe: impl Read + Send + 'static,
        ) -> thread::JoinHandle<std::io::Result<String>> {
            thread::spawn(move || {
                let mut s = String::new();
                pipe.read_to_string(&mut s)?;
                Ok(s)
            })
        }
        let out = drain(child.stdout.take().unwrap());
        let err = drain(child.stderr.take().unwrap());
        let start = Instant::now();
        let limit = if args[0] == "git" { 30 } else { 5 };
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if start.elapsed() < Duration::from_secs(limit) => {
                    thread::sleep(Duration::from_millis(10))
                }
                result => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(if result.is_err() {
                        Error::Unavailable
                    } else {
                        Error::Timeout
                    });
                }
            }
        };
        // A descendant may retain pipe handles: never wait indefinitely for EOF.
        while !out.is_finished() || !err.is_finished() {
            if start.elapsed() >= Duration::from_secs(limit) {
                return Err(Error::Timeout);
            }
            thread::sleep(Duration::from_millis(10));
        }
        Ok(Output {
            code: status.code().unwrap_or(-1),
            out: out
                .join()
                .map_err(|_| Error::Unavailable)?
                .map_err(|_| Error::Unavailable)?,
            err: err
                .join()
                .map_err(|_| Error::Unavailable)?
                .map_err(|_| Error::Unavailable)?,
        })
    }
}
