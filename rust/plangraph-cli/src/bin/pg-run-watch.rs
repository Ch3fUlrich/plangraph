//! Encoding-independent local run launcher and single-probe watcher.
use plangraph_cli::atomic;
use std::{
    ffi::OsString,
    fs::{self, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const QUIET: Duration = Duration::from_secs(60 * 60);

fn sidecar(log: &Path, suffix: &str) -> PathBuf {
    let mut name = log.as_os_str().to_os_string();
    name.push(suffix);
    name.into()
}

fn launch(log: &Path, argv: &[OsString]) -> io::Result<i32> {
    let marker = std::path::absolute(sidecar(log, ".exit"))?;
    let _guard = atomic::lock(&marker)?;
    if marker.try_exists()? {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "exit marker exists",
        ));
    }
    // Unique log names bind markers to runs and prevent stale completion reuse.
    let output = OpenOptions::new().write(true).create_new(true).open(log)?;
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdout(Stdio::from(output.try_clone()?))
        .stderr(Stdio::from(output))
        .spawn()?;
    // A disconnected observer must not prevent waiting or recording completion.
    let _ = writeln!(io::stdout(), "STARTED");
    let _ = io::stdout().flush();
    let status = child.wait()?;
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        status.code().or_else(|| status.signal().map(|s| 128 + s))
    };
    #[cfg(not(unix))]
    let code = status.code();
    let code = code.ok_or_else(|| io::Error::other("child has no exit status"))?;
    // The shared helper publishes only a complete UTF-8 record.
    atomic::write(&marker, format!("EXIT {code}\n").as_bytes())?;
    Ok(code)
}

#[derive(serde::Serialize, serde::Deserialize)]
struct Observation {
    len: u64,
    modified: String,
    changed: u64,
}

fn quiet_for(log: &Path, now: SystemTime) -> io::Result<Duration> {
    let state = std::path::absolute(sidecar(log, ".watch"))?;
    let _guard = atomic::lock(&state)?;
    let metadata = fs::metadata(log)?;
    let modified = metadata
        .modified()?
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let now = now
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs();
    let mut observation = Observation {
        len: metadata.len(),
        modified: modified.as_nanos().to_string(),
        changed: modified.as_secs().min(now),
    };
    match fs::File::open(&state) {
        Ok(file) => {
            let mut text = String::new();
            file.take(2049).read_to_string(&mut text)?;
            let previous: Observation = toml::from_str(&text).map_err(io::Error::other)?;
            observation.changed =
                if previous.len == observation.len && previous.modified == observation.modified {
                    previous.changed.min(now)
                } else {
                    now
                };
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        Err(error) => return Err(error),
    }
    atomic::write(
        &state,
        toml::to_string(&observation)
            .map_err(io::Error::other)?
            .as_bytes(),
    )?;
    Ok(Duration::from_secs(now.saturating_sub(observation.changed)))
}

fn check(log: &Path, now: SystemTime) -> io::Result<(&'static str, i32)> {
    match fs::File::open(sidecar(log, ".exit")) {
        Ok(file) => {
            let mut text = String::new();
            file.take(65).read_to_string(&mut text)?;
            let code = text
                .strip_prefix("EXIT ")
                .and_then(|s| s.strip_suffix('\n'))
                .and_then(|s| s.parse::<i32>().ok())
                .filter(|code| text == format!("EXIT {code}\n"))
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid exit marker"))?;
            println!("EXIT {code}");
            return Ok(("", 0));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        Err(error) => return Err(error),
    }
    let elapsed = quiet_for(log, now)?;
    Ok(if elapsed >= QUIET {
        ("NO_CHANGE 60 minutes", 3)
    } else {
        ("RUNNING", 1)
    })
}

fn run(args: &[OsString]) -> io::Result<i32> {
    match args {
        [mode, log, separator, argv @ ..]
            if mode == "launch" && separator == "--" && !argv.is_empty() =>
        {
            launch(Path::new(log), argv)
        }
        [mode, log] if mode == "check" => {
            let (message, code) = check(Path::new(log), SystemTime::now())?;
            if !message.is_empty() {
                println!("{message}");
            }
            Ok(code)
        }
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: pg-run-watch launch LOG -- PROGRAM [ARG ...] | check LOG",
        )),
    }
}

fn main() {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let code = match run(&args) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("pg-run-watch: {error}");
            2
        }
    };
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quiet_warning_boundary_does_not_reset_on_repeated_probes() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("quiet.log");
        let start = UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        let file = fs::File::create(&log).unwrap();
        file.set_modified(start).unwrap();
        assert!(quiet_for(&log, start + QUIET - Duration::from_secs(1)).unwrap() < QUIET);
        assert_eq!(quiet_for(&log, start + QUIET).unwrap(), QUIET);
        assert!(quiet_for(&log, start + QUIET + Duration::from_secs(1)).unwrap() > QUIET);
    }
}
