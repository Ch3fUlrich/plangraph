//! Bounded subprocess capture shared by the command ports.
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub fn command(name: &str) -> Command {
    let dirs: Vec<_> =
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).collect();
    Command::new(crate::path_check::find(name, &dirs).unwrap_or_else(|| name.into()))
}

pub fn capture(cmd: &mut Command, timeout: u64) -> Result<Output, String> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| match e.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError".to_owned(),
        _ => "OSError".to_owned(),
    })?;
    let out = child.stdout.take().unwrap();
    let err = child.stderr.take().unwrap();
    let read = |mut f: Box<dyn Read + Send>| {
        let mut buf = vec![];
        let _ = f.read_to_end(&mut buf);
        String::from_utf8_lossy(&buf).replace("\r\n", "\n")
    };
    let out = std::thread::spawn(move || read(Box::new(out)));
    let err = std::thread::spawn(move || read(Box::new(err)));
    let start = Instant::now();
    let result = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status.code().unwrap_or(1)),
            Err(_) => break Err("OSError".to_owned()),
            _ if start.elapsed() >= Duration::from_secs(timeout) => {
                break Err("TimeoutExpired".to_owned())
            }
            _ => std::thread::sleep(Duration::from_millis(10)),
        }
    };
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    // A timed-out shell may have children holding the pipes. Dropping the join
    // handles avoids extending the caller's deadline to those children's lives.
    let code = result?;
    Ok(Output {
        code,
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

pub fn run(name: &str, args: &[&str], timeout: u64) -> Result<Output, String> {
    capture(command(name).args(args), timeout)
}
