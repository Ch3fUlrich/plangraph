//! Host capacity only; attempt outcomes live in `probe`.

use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

/// Host capacity snapshot. On Windows, swap is an estimate: total is commit
/// limit minus physical RAM (ullTotalPageFile - ullTotalPhys), not actual
/// pagefile size. Free swap similarly subtracts available RAM from available
/// commit; both subtractions saturate at zero.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct HostProbe {
    pub host_id: String,
    pub os: String,
    pub mem_total_mb: u64,
    pub mem_available_mb: u64,
    pub swap_total_mb: u64,
    pub swap_free_mb: u64,
    pub cpus: usize,
    pub load1: Option<f64>,
    pub load5: Option<f64>,
    pub load15: Option<f64>,
    pub ts_ms: u64,
}

#[derive(Debug, PartialEq, Eq)]
pub struct MemInfo {
    pub mem_total_mb: u64,
    pub mem_available_mb: u64,
    pub swap_total_mb: u64,
    pub swap_free_mb: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum HostProbeError {
    #[error("missing or invalid {0}")]
    Invalid(&'static str),
    #[error("host capacity I/O failed")]
    Io(#[from] std::io::Error),
    #[error("system clock is outside the supported range")]
    Clock,
    #[error("unsupported operating system")]
    Unsupported,
}

/// Parse required Linux kB counters, rounding down to MiB. Never infer availability.
pub fn parse_meminfo(text: &str) -> Result<MemInfo, HostProbeError> {
    let counter = |key: &'static str| -> Result<u64, HostProbeError> {
        let value = text
            .lines()
            .find_map(|line| line.strip_prefix(key))
            .ok_or(HostProbeError::Invalid(key))?;
        let mut fields = value.split_whitespace();
        let kb = fields
            .next()
            .and_then(|s| s.parse::<u64>().ok())
            .ok_or(HostProbeError::Invalid(key))?;
        if fields.next() != Some("kB") || fields.next().is_some() {
            return Err(HostProbeError::Invalid(key));
        }
        Ok(kb / 1024)
    };
    Ok(MemInfo {
        mem_total_mb: counter("MemTotal:")?,
        mem_available_mb: counter("MemAvailable:")?,
        swap_total_mb: counter("SwapTotal:")?,
        swap_free_mb: counter("SwapFree:")?,
    })
}

pub fn parse_loadavg(text: &str) -> Result<(f64, f64, f64), HostProbeError> {
    let mut fields = text.split_whitespace();
    let mut next = || {
        fields
            .next()
            .and_then(|s| s.parse::<f64>().ok())
            .filter(|n| n.is_finite() && *n >= 0.0)
            .ok_or(HostProbeError::Invalid("loadavg"))
    };
    Ok((next()?, next()?, next()?))
}

#[cfg(target_os = "linux")]
fn capacity() -> Result<(MemInfo, [Option<f64>; 3]), HostProbeError> {
    capacity_at(std::path::Path::new("/proc"), true)
}

/// Linux counters under `proc_root`. Memory is required; the load average is required only
/// when `load_required`, otherwise an unreadable or malformed `loadavg` reads as no load.
#[cfg(target_os = "linux")]
fn capacity_at(
    proc_root: &std::path::Path,
    load_required: bool,
) -> Result<(MemInfo, [Option<f64>; 3]), HostProbeError> {
    let mem = parse_meminfo(&std::fs::read_to_string(proc_root.join("meminfo"))?)?;
    let load = std::fs::read_to_string(proc_root.join("loadavg"))
        .map_err(HostProbeError::from)
        .and_then(|text| parse_loadavg(&text));
    match load {
        Ok((a, b, c)) => Ok((mem, [Some(a), Some(b), Some(c)])),
        Err(error) if load_required => Err(error),
        Err(_) => Ok((mem, [None; 3])),
    }
}

#[cfg(windows)]
fn capacity() -> Result<(MemInfo, [Option<f64>; 3]), HostProbeError> {
    use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
    // SAFETY: MEMORYSTATUSEX is a plain C data structure; zero is valid for its fields.
    let mut status: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
    status.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
    // SAFETY: status is initialized, sized, and writable for the duration of the call.
    if unsafe { GlobalMemoryStatusEx(&mut status) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    // Windows pagefile counters describe commit limits, so swap below is only
    // an estimate, not a measurement of pagefile size (including disabled files).
    let mb = 1024 * 1024;
    Ok((
        MemInfo {
            mem_total_mb: status.ullTotalPhys / mb,
            mem_available_mb: status.ullAvailPhys / mb,
            swap_total_mb: status.ullTotalPageFile.saturating_sub(status.ullTotalPhys) / mb,
            swap_free_mb: status.ullAvailPageFile.saturating_sub(status.ullAvailPhys) / mb,
        },
        [None; 3],
    ))
}

#[cfg(not(any(target_os = "linux", windows)))]
fn capacity() -> Result<(MemInfo, [Option<f64>; 3]), HostProbeError> {
    Err(HostProbeError::Unsupported)
}

pub fn probe(host_id: &str) -> Result<HostProbe, HostProbeError> {
    probe_with(host_id, capacity()?)
}

/// Like `probe`, but reads `proc_root` instead of `/proc` and reports `load1: None` when the
/// load average cannot be read, so a caller that only paces on load still gets valid memory.
/// Where there is no Linux `/proc` this is `probe`.
pub fn probe_load_optional(
    host_id: &str,
    proc_root: &std::path::Path,
) -> Result<HostProbe, HostProbeError> {
    probe_with(host_id, optional_capacity(proc_root)?)
}

/// Like `probe_load_optional`, with CPU count and timestamp supplied by the caller.
pub fn probe_load_optional_at(
    host_id: &str,
    proc_root: &std::path::Path,
    cpus: usize,
    ts_ms: u64,
) -> Result<HostProbe, HostProbeError> {
    Ok(with_capacity(
        host_id,
        optional_capacity(proc_root)?,
        cpus,
        ts_ms,
    ))
}

fn optional_capacity(
    proc_root: &std::path::Path,
) -> Result<(MemInfo, [Option<f64>; 3]), HostProbeError> {
    #[cfg(target_os = "linux")]
    return capacity_at(proc_root, false);
    #[cfg(not(target_os = "linux"))]
    {
        let _ = proc_root;
        capacity()
    }
}

fn probe_with(
    host_id: &str,
    capacity: (MemInfo, [Option<f64>; 3]),
) -> Result<HostProbe, HostProbeError> {
    let cpus = std::thread::available_parallelism()?.get();
    let ts_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HostProbeError::Clock)?
        .as_millis()
        .try_into()
        .map_err(|_| HostProbeError::Clock)?;
    Ok(with_capacity(host_id, capacity, cpus, ts_ms))
}

fn with_capacity(
    host_id: &str,
    (mem, [load1, load5, load15]): (MemInfo, [Option<f64>; 3]),
    cpus: usize,
    ts_ms: u64,
) -> HostProbe {
    HostProbe {
        host_id: host_id.to_owned(),
        os: std::env::consts::OS.to_owned(),
        mem_total_mb: mem.mem_total_mb,
        mem_available_mb: mem.mem_available_mb,
        swap_total_mb: mem.swap_total_mb,
        swap_free_mb: mem.swap_free_mb,
        cpus,
        load1,
        load5,
        load15,
        ts_ms,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MEM: &str = "MemTotal:       16385000 kB\nMemFree: 100 kB\nMemAvailable:    8192500 kB\nBuffers: 123 kB\nSwapTotal: 2097152 kB\nSwapFree: 1048576 kB\n";

    #[test]
    fn meminfo_fixture() {
        assert_eq!(
            parse_meminfo(MEM).unwrap(),
            MemInfo {
                mem_total_mb: 16000,
                mem_available_mb: 8000,
                swap_total_mb: 2048,
                swap_free_mb: 1024,
            }
        );
    }

    #[test]
    fn meminfo_requires_available() {
        assert!(parse_meminfo(&MEM.replace("MemAvailable:", "Missing:")).is_err());
    }

    #[test]
    fn meminfo_requires_every_counter() {
        for key in ["MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"] {
            assert!(matches!(parse_meminfo(&MEM.replace(key, "Missing:")),
                Err(HostProbeError::Invalid(missing)) if missing == key));
        }
    }

    #[test]
    fn meminfo_requires_exact_kb_unit() {
        for key in ["MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"] {
            for unit in ["B", "", "KB", "kB extra"] {
                let text = MEM
                    .lines()
                    .map(|line| {
                        if line.starts_with(key) {
                            format!("{key} 1024 {unit}")
                        } else {
                            line.to_owned()
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                assert!(parse_meminfo(&text).is_err());
            }
        }
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "live capacity smoke test; requires host capacity access"]
    fn windows_capacity_shape() {
        let (mem, loads) = capacity().unwrap();
        assert!(mem.mem_total_mb > 0);
        assert!(mem.mem_available_mb <= mem.mem_total_mb);
        assert_eq!(loads, [None; 3]);
    }

    #[test]
    fn meminfo_rejects_garbage() {
        for value in ["garbage", "-1", "18446744073709551616"] {
            assert!(parse_meminfo(&MEM.replace("8192500", value)).is_err());
        }
    }

    #[test]
    fn loadavg_fixture() {
        assert_eq!(
            parse_loadavg("0.12 1.34 2.56 1/234 567\n").unwrap(),
            (0.12, 1.34, 2.56)
        );
    }

    #[test]
    fn loadavg_rejects_garbage() {
        for text in ["garbage", "1 2", "NaN 1 2", "1 inf 2", "-1 0 0"] {
            assert!(parse_loadavg(text).is_err());
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn load_is_optional_only_for_the_optional_probe() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("meminfo"), MEM).unwrap();
        for loadavg in [None, Some("garbage")] {
            if let Some(text) = loadavg {
                std::fs::write(dir.path().join("loadavg"), text).unwrap();
            }
            assert!(capacity_at(dir.path(), true).is_err());
            let p = probe_load_optional("h", dir.path()).unwrap();
            assert_eq!((p.mem_available_mb, p.load1), (8000, None));
        }
        std::fs::write(dir.path().join("loadavg"), "1.5 1 1 1/2 3\n").unwrap();
        assert_eq!(
            probe_load_optional("h", dir.path()).unwrap().load1,
            Some(1.5)
        );
        std::fs::remove_file(dir.path().join("meminfo")).unwrap();
        assert!(probe_load_optional("h", dir.path()).is_err());
    }

    #[test]
    #[cfg(target_os = "linux")]
    #[ignore = "live /proc smoke test; requires an unrestricted Linux host"]
    fn live_host_probe() {
        let result = probe("code").unwrap();
        assert!(result.mem_available_mb <= result.mem_total_mb);
        assert!(result.cpus >= 1);
    }
}
