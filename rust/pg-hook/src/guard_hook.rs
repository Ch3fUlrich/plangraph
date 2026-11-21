//! Run a synchronous policy hook and turn infrastructure failure into a denial.
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

const OUTPUT_LIMIT: u64 = 1024 * 1024;

struct GuardOutput {
    code: i32,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

fn state_dir() -> io::Result<PathBuf> {
    let base = std::env::var_os("FLEET_STATE_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("XDG_STATE_HOME").map(|path| PathBuf::from(path).join("fleet"))
        })
        .or_else(|| {
            std::env::var_os("HOME")
                .or_else(|| std::env::var_os("USERPROFILE"))
                .map(|path| PathBuf::from(path).join(".local/state/fleet"))
        })
        .ok_or_else(|| io::Error::other("fleet state directory unavailable"))?;
    Ok(base.join("guard-outages"))
}

fn run(
    program: &OsString,
    args: &[OsString],
    timeout: Duration,
) -> Result<GuardOutput, &'static str> {
    let stdout = tempfile::tempfile().map_err(|_| "capture unavailable")?;
    let stderr = tempfile::tempfile().map_err(|_| "capture unavailable")?;
    let guard = Path::new(program);
    if !guard.is_file() {
        return Err("guard file unavailable");
    }
    let mut command = if guard.extension().is_some_and(|extension| extension == "py") {
        let mut python = Command::new(if cfg!(windows) { "python" } else { "python3" });
        python.arg(guard);
        python
    } else {
        Command::new(guard)
    };
    let mut child = command
        .args(args)
        .env("PLANGRAPH_GUARD_FAIL_CLOSED", "1")
        .stdout(stdout.try_clone().map_err(|_| "capture unavailable")?)
        .stderr(stderr.try_clone().map_err(|_| "capture unavailable")?)
        .spawn()
        .map_err(|_| "cannot start")?;
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            result => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(if result.is_ok() {
                    "timed out"
                } else {
                    "wait failed"
                });
            }
        }
    };
    let code = status
        .code()
        .filter(|code| matches!(code, 0 | 2))
        .ok_or("crashed")?;
    fn read(mut file: File) -> Result<Vec<u8>, &'static str> {
        file.seek(SeekFrom::Start(0))
            .map_err(|_| "capture unreadable")?;
        let mut bytes = Vec::new();
        file.take(OUTPUT_LIMIT + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "capture unreadable")?;
        if bytes.len() as u64 > OUTPUT_LIMIT {
            return Err("output too large");
        }
        Ok(bytes)
    }
    Ok(GuardOutput {
        code,
        stdout: read(stdout)?,
        stderr: read(stderr)?,
    })
}

fn incident(path: &std::path::Path, guard: &str, reason: &str) -> io::Result<bool> {
    fs::create_dir_all(
        path.parent()
            .ok_or_else(|| io::Error::other("missing alert directory"))?,
    )?;
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = match options.open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => return Ok(false),
        Err(error) => return Err(error),
    };
    writeln!(file, "GUARD-OUTAGE {guard} {reason}")?;
    file.sync_all()?;
    Ok(true)
}

fn run_hook() -> i32 {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let [guard, millis, program, args @ ..] = args.as_slice() else {
        eprintln!("guard-hook: expected GUARD TIMEOUT_MS PROGRAM [ARGS]");
        return 2;
    };
    let Some(guard @ ("bash" | "context")) = guard.to_str() else {
        eprintln!("guard-hook: invalid guard identifier");
        return 2;
    };
    let Some(millis) = millis
        .to_str()
        .and_then(|number| number.parse::<u64>().ok())
        .filter(|number| (1..=60_000).contains(number))
    else {
        eprintln!("guard-hook: invalid guard timeout");
        return 2;
    };
    let marker = state_dir().map(|dir| dir.join(format!("{guard}.alert")));
    let outcome = run(program, args, Duration::from_millis(millis));
    match outcome {
        Ok(GuardOutput {
            code,
            stdout,
            stderr,
        }) => {
            if let Ok(marker) = marker {
                let _ = fs::remove_file(marker);
            }
            if io::stdout().write_all(&stdout).is_err() || io::stderr().write_all(&stderr).is_err()
            {
                return 2;
            }
            code
        }
        Err(reason) => {
            let first = marker
                .and_then(|marker| incident(&marker, guard, reason))
                .unwrap_or(true);
            if first {
                eprintln!("GUARD-OUTAGE {guard} {reason}");
            } else {
                eprintln!("guard-hook: {guard} unavailable");
            }
            2
        }
    }
}

fn main() {
    std::process::exit(run_hook());
}
