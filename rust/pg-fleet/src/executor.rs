use crate::process::Output;
use std::io::{Read, Seek, SeekFrom};
use std::process::Stdio;
pub trait Executor {
    fn run(
        &mut self,
        program: &str,
        args: &[String],
        timeout: u64,
        combined: bool,
    ) -> Result<Output, String>;
    fn pause(&mut self, seconds: u64);
}
pub struct Local;
impl Executor for Local {
    fn run(
        &mut self,
        program: &str,
        args: &[String],
        timeout: u64,
        combined: bool,
    ) -> Result<Output, String> {
        let mut cmd = crate::process::command(program);
        cmd.args(args);
        if !combined {
            return crate::process::capture(&mut cmd, timeout);
        }
        // Both streams share a file description, retaining the order needed
        // by the legacy claim-tail and verifier refusal receipts.
        let mut file = tempfile::tempfile().map_err(|_| "OSError".to_owned())?;
        let out = file.try_clone().map_err(|_| "OSError".to_owned())?;
        let err = file.try_clone().map_err(|_| "OSError".to_owned())?;
        let mut child = cmd
            .stdin(Stdio::null())
            .stdout(out)
            .stderr(err)
            .spawn()
            .map_err(|_| "OSError".to_owned())?;
        let start = std::time::Instant::now();
        let code = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status.code().unwrap_or(1),
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("OSError".into());
                }
                _ if start.elapsed() >= std::time::Duration::from_secs(timeout) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("TimeoutExpired".into());
                }
                _ => std::thread::sleep(std::time::Duration::from_millis(10)),
            }
        };
        file.seek(SeekFrom::Start(0))
            .map_err(|_| "OSError".to_owned())?;
        let mut raw = vec![];
        file.read_to_end(&mut raw)
            .map_err(|_| "OSError".to_owned())?;
        Ok(Output {
            code,
            stdout: String::from_utf8_lossy(&raw).replace("\r\n", "\n"),
            stderr: String::new(),
        })
    }
    fn pause(&mut self, seconds: u64) {
        std::thread::sleep(std::time::Duration::from_secs(seconds));
    }
}
pub fn strings(args: &[&str]) -> Vec<String> {
    args.iter().map(|s| s.to_string()).collect()
}
