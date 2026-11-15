//! Linux/systemd breaker. Keep the timer enabled so a trip can heal on a later tick.
use chrono::{DateTime, Utc};
use pg_worker_core::run::record_io::atomic_write_private;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufRead, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const HALF_OPEN_SECONDS: i64 = 15 * 60;

#[derive(Default, Deserialize, Serialize)]
#[serde(default)]
struct State {
    count: u64,
    sig: String,
    updated: String,
    tripped: bool,
    alerted: bool,
}
fn read(path: &Path) -> Result<State> {
    match fs::read(path) {
        Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(State::default()),
        Err(e) => Err(e.into()),
    }
}
fn save(path: &Path, state: &State) -> Result<()> {
    Ok(atomic_write_private(path, &serde_json::to_vec(state)?)?)
}
fn unit_name(unit: &str) -> Result<()> {
    if unit.is_empty()
        || unit.starts_with('.')
        || !unit
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b))
    {
        return Err("invalid unit name".into());
    }
    Ok(())
}
fn directory() -> Result<PathBuf> {
    let base = match std::env::var_os("XDG_STATE_HOME") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => PathBuf::from(std::env::var_os("HOME").ok_or("HOME is unset")?).join(".local/state"),
    };
    // Retain the old location so existing trips and reset commands remain visible.
    Ok(base.join("autoos/breaker"))
}
fn lock(dir: &Path, unit: &str) -> Result<File> {
    fs::create_dir_all(dir)?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join(format!("{unit}.lock")))?;
    file.try_lock().map_err(|e| format!("breaker busy: {e}"))?;
    Ok(file)
}
fn remove(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}
fn item_path(dir: &Path, unit: &str) -> PathBuf {
    dir.join("needs-you").join(format!("{unit}.json"))
}
fn clear(dir: &Path, unit: &str) -> Result<()> {
    remove(&dir.join(format!("{unit}.tripped")))?;
    remove(&item_path(dir, unit))
}
fn notify(dir: &Path, unit: &str, state: &mut State) -> Result<()> {
    let item = item_path(dir, unit);
    if state.alerted && item.exists() {
        return Ok(());
    }
    // A legacy alerted=true record may lack the item. Keep publication pending
    // until BOTH the durable item and routing line have been written.
    state.alerted = false;
    save(&dir.join(format!("{unit}.json")), state)?;
    let line = format!("{} BREAKER {unit} tripped after {} failures; owner=routing; needs-you={}; half-open after 15min\n",
        state.updated, state.count, item.display());
    atomic_write_private(
        &item,
        &serde_json::to_vec(&json!({
            "kind":"needs-you", "owner":"routing", "unit":unit,
            "updated":state.updated, "action":"Inspect failure; reset immediately if recycling is blocked",
            "reset":format!("unit-breaker reset {unit}")
        }))?,
    )?;
    if let Some(inbox) = std::env::var_os("AUTOOS_ALERT_INBOX") {
        // Do not change an existing routing inbox's permissions.
        let mut file = OpenOptions::new().create(true).append(true).open(inbox)?;
        file.write_all(line.as_bytes())?;
        file.sync_all()?;
    } else {
        io::stderr().write_all(line.as_bytes())?;
    }
    state.alerted = true;
    save(&dir.join(format!("{unit}.json")), state)
}
fn child(cmd: &[String]) -> Result<(i32, String)> {
    let mut child = Command::new(&cmd[0])
        .args(&cmd[1..])
        .stderr(Stdio::piped())
        .spawn()?;
    let mut last = String::new();
    let read_result = (|| -> io::Result<()> {
        let mut reader = io::BufReader::new(child.stderr.take().unwrap());
        let mut bytes = Vec::new();
        while reader.read_until(b'\n', &mut bytes)? != 0 {
            io::stderr().write_all(&bytes)?;
            let line = String::from_utf8_lossy(&bytes);
            if !line.trim().is_empty() {
                last = line.trim().chars().take(200).collect();
            }
            bytes.clear();
        }
        Ok(())
    })();
    let status = child.wait()?;
    read_result?;
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(1))
    };
    #[cfg(not(unix))]
    let code = status.code().unwrap_or(1);
    Ok((code, last))
}
fn failure_class(code: i32, last: &str) -> &'static str {
    if matches!(code, 2 | 3) {
        "startup"
    } else if ["OSError", "WinError", "PermissionError"]
        .iter()
        .any(|s| last.contains(s))
    {
        "io"
    } else if last.contains("MemoryError") || last.contains("floor unreadable") {
        "memory"
    } else if code >= 128 || last.contains("Traceback") || last.contains("Unhandled exception:") {
        "crash"
    } else {
        "other"
    }
}
fn wrap(dir: &Path, unit: &str, threshold: u64, cmd: &[String]) -> Result<i32> {
    let _lock = lock(dir, unit)?;
    let path = dir.join(format!("{unit}.json"));
    let mut state = read(&path)?;
    if state.tripped {
        let updated = DateTime::parse_from_rfc3339(&state.updated)?;
        if Utc::now().signed_duration_since(updated).num_seconds() < HALF_OPEN_SECONDS {
            notify(dir, unit, &mut state)?;
            eprintln!("unit-breaker: {unit} is tripped; half-open after 15min");
            return Ok(1);
        }
    }
    let (code, last) = match child(cmd) {
        Ok(result) => result,
        Err(e) => {
            eprintln!("unit-breaker: child launch/read failed: {e}");
            (1, "OSError".to_owned())
        }
    };
    if matches!(code, 130 | 143) {
        return Ok(code);
    }
    let now = Utc::now().to_rfc3339();
    if code == 0 {
        // Clear durable notifications before the trip, so a failed clear remains visible.
        clear(dir, unit)?;
        save(
            &path,
            &State {
                updated: now,
                ..State::default()
            },
        )?;
        return Ok(0);
    }
    let sig = failure_class(code, &last);
    let count = if state.sig == sig {
        state.count.saturating_add(1)
    } else {
        1
    };
    // A failed half-open run immediately re-trips, even if its failure class changed.
    let tripped = state.tripped || count >= threshold;
    state = State {
        count,
        sig: sig.into(),
        updated: now,
        tripped,
        alerted: state.alerted,
    };
    save(&path, &state)?;
    if tripped {
        notify(dir, unit, &mut state)?;
    }
    Ok(code)
}
fn main_result() -> Result<i32> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let dir = directory()?;
    if args == ["status"] {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(0),
            Err(e) => return Err(e.into()),
        };
        let mut paths = entries
            .map(|e| e.map(|e| e.path()))
            .collect::<io::Result<Vec<_>>>()?;
        paths.sort();
        for path in paths
            .iter()
            .filter(|p| p.extension().is_some_and(|e| e == "json"))
        {
            let state = read(path)?;
            if state.tripped {
                println!(
                    "{} state=tripped count={} sig={}",
                    path.file_stem().unwrap().to_string_lossy(),
                    state.count,
                    state.sig
                );
            }
        }
        return Ok(0);
    }
    if args.len() == 2 && args[0] == "reset" {
        unit_name(&args[1])?;
        let _lock = lock(&dir, &args[1])?;
        clear(&dir, &args[1])?;
        remove(&dir.join(format!("{}.json", args[1])))?;
        return Ok(0);
    }
    let usage =
        "usage: unit-breaker --unit NAME [--threshold N] -- COMMAND... | status | reset NAME";
    if args.len() < 4 || args[0] != "--unit" {
        return Err(usage.into());
    }
    unit_name(&args[1])?;
    let (threshold, sep) = if args[2] == "--threshold" && args.len() >= 6 {
        (args[3].parse::<u64>()?, 4)
    } else {
        (5, 2)
    };
    if threshold == 0 || args[sep] != "--" || args.len() <= sep + 1 {
        return Err(usage.into());
    }
    wrap(&dir, &args[1], threshold, &args[sep + 1..])
}
fn main() {
    let code = match main_result() {
        Ok(code) => code,
        Err(e) => {
            eprintln!("unit-breaker: {e}");
            2
        }
    };
    std::process::exit(code);
}
