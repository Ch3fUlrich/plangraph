//! Stateful breaker port. Persist before disabling a unit (which may kill us).
use crate::atomic;
use serde::{Deserialize, Serialize};
use sha1::{Digest, Sha1};
use std::fs;
use std::io::{self, Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};

#[derive(Default, Deserialize, Serialize)]
pub struct State {
    #[serde(default)]
    pub count: u64,
    #[serde(default)]
    pub sig: String,
    #[serde(default)]
    pub tripped: bool,
    #[serde(default)]
    pub alerted: bool,
    #[serde(default)]
    pub last: String,
}
pub fn valid_unit(unit: &str) -> bool {
    !unit.is_empty()
        && !unit.starts_with('-')
        && unit.len() < 200
        && unit
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b))
        && ![".", ".."].contains(&unit)
}
pub fn signature(code: i32, last: &str) -> String {
    let hex = regex::Regex::new(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8,}\b").unwrap();
    let digits = regex::Regex::new(r"\d+").unwrap();
    let normalized = hex.replace_all(last.trim(), "H");
    let normalized = digits.replace_all(&normalized, "N");
    format!("{code}:{:x}", Sha1::digest(normalized.as_bytes()))
}
pub fn failure_class(code: i32, last: &str) -> String {
    if [2, 3].contains(&code) {
        "startup"
    } else if ["OSError", "WinError", "PermissionError"]
        .iter()
        .any(|s| last.contains(s))
    {
        "io"
    } else if last.contains("MemoryError") || last.contains("floor unreadable") {
        "memory"
    } else if !(0..128).contains(&code)
        || last.contains("Traceback")
        || last.contains("Unhandled exception:")
    {
        "crash"
    } else {
        "other"
    }
    .into()
}
pub fn read(path: &Path) -> io::Result<State> {
    atomic::regular(path)?;
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(io::Error::other),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(State::default()),
        Err(e) => Err(e),
    }
}

pub fn record(
    directory: &Path,
    unit: &str,
    threshold: u64,
    state: &mut State,
    outcome: (i32, &str),
    alert: Option<&Path>,
    mut disable: impl FnMut() -> bool,
) -> io::Result<()> {
    let (code, last) = outcome;
    if [130, 143].contains(&code) {
        return Ok(());
    }
    if code == 0 {
        *state = State::default();
    } else {
        let sig = if cfg!(windows) {
            failure_class(code, last)
        } else {
            signature(code, last)
        };
        state.count = if sig == state.sig {
            state.count.saturating_add(1)
        } else {
            1
        };
        state.sig = sig;
        state.last = last.chars().take(200).collect();
        state.tripped = state.count >= threshold;
    }
    if state.tripped && !state.alerted {
        // The durable flag prevents repeated alerts after service-manager termination.
        state.alerted = true;
        atomic::write(
            &directory.join(format!("{unit}.json")),
            &serde_json::to_vec(state)?,
        )?;
        let line = format!(
            "{} BREAKER {unit} tripped after {} failures\n",
            chrono::Utc::now().to_rfc3339(),
            state.count
        );
        let delivered = alert.is_some_and(|path| {
            (|| -> io::Result<()> {
                atomic::regular(path)?;
                let mut options = fs::OpenOptions::new();
                options.create(true).append(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    options.mode(0o600);
                }
                let mut file = options.open(path)?;
                file.write_all(line.as_bytes())?;
                file.sync_all()
            })()
            .is_ok()
        });
        if !delivered {
            eprint!("{line}");
        }
        if !disable() {
            atomic::write(
                &directory.join(format!("{unit}.tripped")),
                b"disabled unavailable\n",
            )?;
        }
    }
    atomic::write(
        &directory.join(format!("{unit}.json")),
        &serde_json::to_vec(state)?,
    )
}

pub fn wrap(
    directory: &Path,
    unit: &str,
    threshold: u64,
    command: &[String],
    alert: Option<&Path>,
) -> io::Result<i32> {
    if !valid_unit(unit) || threshold == 0 || command.is_empty() {
        return Err(io::Error::other("invalid breaker arguments"));
    }
    fs::create_dir_all(directory)?;
    let path = directory.join(format!("{unit}.json"));
    let _lock = atomic::lock(&path)?;
    let mut state = read(&path)?;
    if state.tripped {
        eprintln!("unit-breaker {unit} refused tripped; reset required");
        return Ok(1);
    }
    let mut child = Command::new(&command[0])
        .args(&command[1..])
        .stderr(Stdio::piped())
        .spawn()?;
    #[cfg(unix)]
    let forward = {
        let mut signals = signal_hook::iterator::Signals::new([
            signal_hook::consts::SIGINT,
            signal_hook::consts::SIGTERM,
        ])?;
        let handle = signals.handle();
        let pid = child.id() as i32;
        let thread = std::thread::spawn(move || {
            for signal in signals.forever() {
                // SAFETY: kill forwards a signal to the still-owned child PID.
                unsafe {
                    libc::kill(pid, signal);
                }
            }
        });
        (handle, thread)
    };
    let mut tail = Vec::new();
    let mut buffer = [0; 4096];
    let mut stderr = child.stderr.take().unwrap();
    loop {
        let size = stderr.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        io::stderr().write_all(&buffer[..size])?;
        tail.extend_from_slice(&buffer[..size]);
        if tail.len() > 8192 {
            tail.drain(..tail.len() - 8192);
        }
    }
    let status = child.wait()?;
    #[cfg(unix)]
    {
        forward.0.close();
        let _ = forward.1.join();
    }
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(1))
    };
    #[cfg(not(unix))]
    let code = status.code().unwrap_or(1);
    let text = String::from_utf8_lossy(&tail);
    let last = text
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("");
    record(
        directory,
        unit,
        threshold,
        &mut state,
        (code, last),
        alert,
        || {
            if cfg!(windows) {
                let task = unit.strip_suffix(".service").unwrap_or(unit);
                let _ = Command::new("schtasks")
                    .args(["/End", "/TN", task])
                    .status();
                Command::new("schtasks")
                    .args(["/Change", "/TN", task, "/DISABLE"])
                    .status()
                    .is_ok_and(|s| s.success())
            } else {
                Command::new("systemctl")
                    .args(["--user", "disable", "--now", unit])
                    .status()
                    .is_ok_and(|s| s.success())
            }
        },
    )?;
    Ok(code)
}
pub fn cli(home: &Path, args: &[String]) -> i32 {
    let directory = crate::paths::state(home).join("breaker");
    let result = (|| -> io::Result<i32> {
        match args
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
            .as_slice()
        {
            ["status"] => {
                if !directory.exists() {
                    return Ok(0);
                }
                let mut paths: Vec<_> = fs::read_dir(&directory)?.collect::<Result<Vec<_>, _>>()?;
                paths.sort_by_key(|e| e.file_name());
                for entry in paths {
                    if entry.path().extension().is_some_and(|s| s == "json") {
                        let state = read(&entry.path())?;
                        println!(
                            "{} state={} count={}",
                            entry
                                .file_name()
                                .to_string_lossy()
                                .trim_end_matches(".json"),
                            if state.tripped {
                                "tripped"
                            } else if state.count > 0 {
                                "failing"
                            } else {
                                "ok"
                            },
                            state.count
                        );
                    }
                }
                Ok(0)
            }
            ["reset", unit] if valid_unit(unit) => {
                fs::create_dir_all(&directory)?;
                let _lock = atomic::lock(&directory.join(format!("{unit}.json")))?;
                for suffix in ["json", "tripped"] {
                    match fs::remove_file(directory.join(format!("{unit}.{suffix}"))) {
                        Ok(()) => (),
                        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
                        Err(e) => return Err(e),
                    }
                }
                Ok(0)
            }
            _ => {
                let split = args
                    .iter()
                    .position(|a| a == "--")
                    .ok_or_else(|| io::Error::other("missing command"))?;
                let mut unit = None;
                let mut threshold = 5;
                let (options, remainder) = args[..split].as_chunks::<2>();
                for pair in options {
                    match pair[0].as_str() {
                        "--unit" => unit = Some(pair[1].as_str()),
                        "--threshold" => threshold = pair[1].parse().map_err(io::Error::other)?,
                        _ => return Err(io::Error::other("unknown option")),
                    }
                }
                if !remainder.is_empty() {
                    return Err(io::Error::other("missing option value"));
                }
                let alert = std::env::var_os("PLANGRAPH_ALERT_INBOX").map(std::path::PathBuf::from);
                wrap(
                    &directory,
                    unit.ok_or_else(|| io::Error::other("missing unit"))?,
                    threshold,
                    &args[split + 1..],
                    alert.as_deref(),
                )
            }
        }
    })();
    result.unwrap_or_else(|_| {
        eprintln!("unit-breaker refused invalid arguments, state or execution");
        2
    })
}
