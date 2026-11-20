use crate::process::{Error, Runner};
use serde_json::Value;
use std::{fs, path::Path};

fn diagnostic(error: Error) -> &'static str {
    if error == Error::Timeout {
        "timed out"
    } else {
        "unavailable"
    }
}
fn interpreter(exe: &str) -> bool {
    let exe = exe.rsplit(['/', '\\']).next().unwrap_or("").to_lowercase();
    let exe = exe.strip_suffix(".exe").unwrap_or(&exe);
    ["pythonw", "python", "pypy"].iter().any(|prefix| {
        exe.strip_prefix(prefix).is_some_and(|v| {
            v.strip_suffix('t')
                .unwrap_or(v)
                .chars()
                .all(|c| c.is_ascii_digit() || c == '.')
        })
    })
}
fn target(arg: &str) -> bool {
    let arg = arg.replace('\\', "/");
    arg == "plangraph/keeper/daemon.py" || arg.ends_with("/plangraph/keeper/daemon.py")
}
pub fn keeper_launch(exe: &str, command: &str, windows: bool) -> bool {
    if !interpreter(exe) {
        return false;
    }
    // Preserve Windows path separators; quotes delimit paths with spaces.
    let parsed = if windows {
        let mut args = Vec::new();
        let mut current = String::new();
        let mut quoted = false;
        for c in command.chars() {
            match c {
                '"' => quoted = !quoted,
                c if c.is_whitespace() && !quoted => {
                    if !current.is_empty() {
                        args.push(std::mem::take(&mut current));
                    }
                }
                _ => current.push(c),
            }
        }
        if quoted {
            return false;
        }
        if !current.is_empty() {
            args.push(current);
        }
        Some(args)
    } else {
        shlex::split(command)
    };
    let Some(args) = parsed else {
        return false;
    };
    let mut args = args.iter().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-c" | "-" => return false,
            "-m" => {
                return args.next().is_some_and(|v| {
                    matches!(v.as_str(), "plangraph.keeper.daemon" | "plangraph.keeper")
                })
            }
            "-X" | "-W" => {
                args.next();
            }
            "--" => return args.next().is_some_and(|v| target(v)),
            v if !v.starts_with('-') => return target(v),
            _ => {}
        }
    }
    false
}
pub fn process_pid(
    run: &impl Runner,
    windows: bool,
    match_pid: Option<u32>,
) -> Result<u32, String> {
    let args = if windows {
        vec!["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"]
    } else {
        vec!["ps", "-A", "-o", "pid=", "-o", "comm=", "-o", "args="]
    };
    let output = run
        .run(&args)
        .map_err(|e| format!("keeper process query {}", diagnostic(e)))?;
    if output.code != 0 {
        return Err("keeper process query failed".into());
    }
    let mut entries = Vec::new();
    let invalid = || "keeper process query unavailable".to_owned();
    if windows {
        let rows: Value = serde_json::from_str(if output.out.trim().is_empty() {
            "[]"
        } else {
            &output.out
        })
        .map_err(|_| invalid())?;
        let rows = if rows.is_object() {
            vec![rows]
        } else {
            rows.as_array().ok_or_else(invalid)?.clone()
        };
        for row in rows {
            let pid = row["ProcessId"]
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
                .ok_or_else(invalid)?;
            entries.push((
                pid,
                row["Name"].as_str().ok_or_else(invalid)?.to_owned(),
                row["CommandLine"].as_str().unwrap_or("").to_owned(),
            ));
        }
    } else {
        for line in output.out.lines().filter(|s| !s.trim().is_empty()) {
            let mut parts = line.trim().splitn(2, char::is_whitespace);
            let pid = parts
                .next()
                .unwrap_or("")
                .parse::<u32>()
                .map_err(|_| invalid())?;
            let mut rest = parts
                .next()
                .ok_or_else(invalid)?
                .trim()
                .splitn(2, char::is_whitespace);
            entries.push((
                pid,
                rest.next().unwrap_or("").to_owned(),
                rest.next().unwrap_or("").trim().to_owned(),
            ));
        }
    }
    for (pid, exe, args) in entries {
        if pid != 0
            && pid != std::process::id()
            && match_pid.is_none_or(|wanted| wanted == pid)
            && keeper_launch(&exe, &args, windows)
        {
            return Ok(pid);
        }
    }
    Ok(0)
}
pub fn main_pid(run: &impl Runner, unit: &str) -> Result<u32, String> {
    let mut errors = Vec::new();
    for scope in ["user", "system"] {
        let mut args = vec!["systemctl"];
        if scope == "user" {
            args.push("--user");
        }
        args.extend(["show", unit, "--property=LoadState", "--property=MainPID"]);
        let output = match run.run(&args) {
            Ok(output) => output,
            Err(Error::Missing) => break,
            Err(e) => {
                errors.push(format!("keeper {scope} unit query {}", diagnostic(e)));
                continue;
            }
        };
        let fields: std::collections::HashMap<_, _> = output
            .out
            .lines()
            .filter_map(|s| s.split_once('='))
            .collect();
        if fields.get("LoadState") == Some(&"not-found") {
            continue;
        }
        if output.code != 0 {
            errors.push(format!(
                "keeper {scope} unit {}",
                if output.err.to_lowercase().contains("bus") {
                    "bus unreachable"
                } else {
                    "query failed"
                }
            ));
            continue;
        }
        match fields.get("MainPID").and_then(|s| s.parse::<u32>().ok()) {
            Some(pid) if fields.get("LoadState") == Some(&"loaded") => {
                if pid != 0 {
                    return Ok(pid);
                }
            }
            _ => errors.push(format!("keeper {scope} unit state unreadable")),
        }
    }
    let pid = process_pid(run, cfg!(windows), None)?;
    if pid != 0 || errors.is_empty() {
        Ok(pid)
    } else {
        Err(errors.join("; "))
    }
}
pub fn pid_start(pid: u32) -> Option<String> {
    fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()?
        .rsplit_once(')')?
        .1
        .split_whitespace()
        .nth(19)
        .map(str::to_owned)
}
pub fn running(path: &Path, run: &impl Runner, windows: bool) -> Result<Option<String>, String> {
    let text = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("invalid keeper loaded state".into()),
    };
    let row: Value = serde_json::from_str(&text).map_err(|_| "invalid keeper loaded state")?;
    let pid = row["pid"]
        .as_u64()
        .and_then(|p| u32::try_from(p).ok())
        .filter(|p| *p > 0 && *p <= i32::MAX as u32)
        .ok_or("invalid keeper loaded state")?;
    if windows {
        if process_pid(run, true, Some(pid))? != pid {
            return Ok(None);
        }
    } else {
        #[cfg(unix)]
        // Signal zero only on POSIX; Windows always uses the read-only CIM query.
        if unsafe { libc::kill(pid as i32, 0) } != 0
            && std::io::Error::last_os_error().raw_os_error() != Some(libc::EPERM)
        {
            return Ok(None);
        }
    }
    if let (Some(saved), Some(actual)) = (row["pid_start"].as_str(), pid_start(pid)) {
        if saved != actual {
            return Ok(None);
        }
    }
    let sha = row["loaded_sha"]
        .as_str()
        .filter(|s| {
            matches!(s.len(), 40 | 64)
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .ok_or("keeper loaded SHA unavailable")?;
    Ok(Some(sha.into()))
}
#[cfg(test)]
mod tests;
