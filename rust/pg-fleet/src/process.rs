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
    let (send, receive) = std::sync::mpsc::channel();
    let err_send = send.clone();
    std::thread::spawn(move || {
        let _ = send.send((false, read(Box::new(out))));
    });
    std::thread::spawn(move || {
        let _ = err_send.send((true, read(Box::new(err))));
    });
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
    // Descendants may retain pipes after their parent exits. Reading both
    // streams remains subject to the same deadline as waiting for the parent.
    let code = result?;
    let mut stdout = String::new();
    let mut stderr = String::new();
    for _ in 0..2 {
        let remaining = Duration::from_secs(timeout).saturating_sub(start.elapsed());
        let (is_err, text) = receive
            .recv_timeout(remaining)
            .map_err(|_| "TimeoutExpired".to_owned())?;
        if is_err {
            stderr = text
        } else {
            stdout = text
        }
    }
    Ok(Output {
        code,
        stdout,
        stderr,
    })
}

pub fn run(name: &str, args: &[&str], timeout: u64) -> Result<Output, String> {
    capture(command(name).args(args), timeout)
}
