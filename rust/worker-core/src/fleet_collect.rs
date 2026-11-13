//! Local capacity collection, reusing the worker host probe.
use crate::{
    fleet_series::{Rss, Sample},
    host_probe,
};
use serde_json::Value;
use std::{fs, io, path::Path, process::Command};

pub fn collect(host: &str, state: &Path) -> io::Result<Sample> {
    let probe = host_probe::probe(host).map_err(io::Error::other)?;
    let mut sample = Sample {
        host: host.into(),
        ts: probe.ts_ms / 1000,
        cpu_load: probe.load1,
        mem_avail_mb: Some(probe.mem_available_mb),
        swap_used_mb: Some(probe.swap_total_mb.saturating_sub(probe.swap_free_mb)),
        cores: probe.cpus,
        reachable: true,
        ..Sample::default()
    };
    if let Ok(text) = fs::read_to_string("/proc/vmstat") {
        sample.oom_kill = text
            .lines()
            .find_map(|l| l.strip_prefix("oom_kill ")?.trim().parse().ok());
    }
    if let Ok(text) = fs::read_to_string("/proc/pressure/memory") {
        sample.psi_mem_avg10 = text.lines().find(|l| l.starts_with("some ")).and_then(|l| {
            l.split_whitespace()
                .find_map(|v| v.strip_prefix("avg10=")?.parse().ok())
        });
    }
    sample.disk_pct = disk_pct(state);
    sample.top_rss = top_rss();
    match fs::read_dir(state.join("runs")) {
        Ok(entries) => {
            for entry in entries {
                let path = entry?.path().join("record.json");
                if !path.is_file() {
                    continue;
                }
                let row: Value =
                    serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)?;
                if row["host"].as_str() != Some(host) {
                    continue;
                }
                match row["status"].as_str() {
                    Some("queued") => sample.ready += 1,
                    Some("running") => {
                        sample.runs += 1;
                        if let Some(id) = row["session_id"]
                            .as_str()
                            .or_else(|| row["session"].as_str())
                        {
                            sample.sessions.insert(id.into());
                        }
                    }
                    _ => {}
                }
            }
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    Ok(sample)
}

pub fn disk_pct(path: &Path) -> Option<f64> {
    #[cfg(unix)]
    {
        let output = Command::new("df")
            .arg("-Pk")
            .arg(path)
            .env("LC_ALL", "C")
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let text = String::from_utf8(output.stdout).ok()?;
        text.lines()
            .last()?
            .split_whitespace()
            .nth(4)?
            .trim_end_matches('%')
            .parse()
            .ok()
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let (mut available, mut total, mut free) = (0u64, 0u64, 0u64);
        // SAFETY: the path is nul-terminated and all output pointers are writable.
        let ok = unsafe {
            windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
                wide.as_ptr(),
                &mut available,
                &mut total,
                &mut free,
            )
        };
        (ok != 0 && total > 0)
            .then(|| 100.0 * (total.saturating_sub(available)) as f64 / total as f64)
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = path;
        None
    }
}

fn top_rss() -> Vec<Rss> {
    let mut rows = Vec::new();
    if let Ok(entries) = fs::read_dir("/proc") {
        for entry in entries.flatten() {
            let Some(pid) = entry.file_name().to_str().and_then(|s| s.parse().ok()) else {
                continue;
            };
            let Ok(text) = fs::read_to_string(entry.path().join("status")) else {
                continue;
            };
            let Some(rss) = text.lines().find_map(|s| {
                s.strip_prefix("VmRSS:")?
                    .split_whitespace()
                    .next()?
                    .parse::<u64>()
                    .ok()
            }) else {
                continue;
            };
            let name = text
                .lines()
                .find_map(|s| s.strip_prefix("Name:"))
                .unwrap_or("")
                .trim()
                .into();
            rows.push(Rss {
                pid,
                name,
                rss_mb: rss / 1024,
            });
        }
    }
    rows.sort_by_key(|r| std::cmp::Reverse(r.rss_mb));
    rows.truncate(5);
    rows
}

pub fn remote(host: &str, destination: &str) -> io::Result<Sample> {
    if !crate::fleet_series::valid_host(host)
        || destination.starts_with('-')
        || destination.contains(char::is_whitespace)
    {
        return Err(io::Error::other("invalid host route"));
    }
    let output = crate::host_command::output(
        Command::new("ssh").args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            destination,
            "pg-fleet-series",
            "probe",
            "--host",
            host,
        ]),
        std::time::Duration::from_secs(15),
    )?;
    let row: Sample = serde_json::from_slice(&output).map_err(io::Error::other)?;
    if row.host != host {
        return Err(io::Error::other("host probe identity mismatch"));
    }
    Ok(row)
}
