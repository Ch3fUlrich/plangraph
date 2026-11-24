//! Free RAM on a WSL host. WSL's `MemAvailable` counts memory the Windows side already uses,
//! so admission takes the lower of it and Windows' own free physical memory (read through WSL
//! interop), less a Windows floor. A failed read falls back to the last value for one facts
//! window, then refuses: a host that cannot see its Windows side admits nothing.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Longest wait for the PowerShell read before the host falls back to the cached value.
const READ_TIMEOUT: Duration = Duration::from_secs(15);
const CACHE_FILE: &str = "windows-free-ram.json";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct WindowsRam {
    pub free_bytes: u64,
    /// `live` for a fresh read, `cached` for the last value after a failed read.
    pub source: &'static str,
}

/// WSL shows in the kernel version string (`microsoft`) and sets `WSL_INTEROP` in sessions.
pub(super) fn is_wsl(proc_version: &str, interop: Option<&str>) -> bool {
    interop.is_some_and(|v| !v.is_empty())
        || proc_version.to_ascii_lowercase().contains("microsoft")
}

/// `FreePhysicalMemory` is reported in KiB.
fn parse_free_kb(out: &str) -> Result<u64, String> {
    out.trim()
        .parse::<u64>()
        .map(|kb| kb.saturating_mul(1024))
        .map_err(|_| {
            format!(
                "unexpected powershell output {:?}",
                out.trim().chars().take(40).collect::<String>()
            )
        })
}

/// Windows' free physical memory in bytes via `powershell.exe`, killed after `READ_TIMEOUT`.
pub(super) fn read_powershell() -> Result<u64, String> {
    let mut child = Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("cannot start powershell.exe: {e}"))?;
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(status)) => return Err(format!("powershell.exe exited with {status}")),
            Ok(None) if start.elapsed() > READ_TIMEOUT => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "powershell.exe timed out after {}s",
                    READ_TIMEOUT.as_secs()
                ));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(format!("powershell.exe wait failed: {e}")),
        }
    }
    let out = child
        .wait_with_output()
        .map_err(|e| format!("powershell.exe output: {e}"))?;
    parse_free_kb(&String::from_utf8_lossy(&out.stdout))
}

/// The Windows free RAM from `read`; a failed read uses the cached last value while it is at
/// most `max_age` seconds old and otherwise refuses with `windows_ram_unreadable`. A live
/// value is cached unless `dry` (a dry run writes no state).
pub(super) fn read_with_cache(
    read: impl FnOnce() -> Result<u64, String>,
    cache_dir: &Path,
    now: i64,
    max_age: i64,
    dry: bool,
) -> Result<WindowsRam, String> {
    let cache = cache_dir.join(CACHE_FILE);
    match read() {
        Ok(free_bytes) => {
            if !dry {
                let row = serde_json::json!({"free_bytes":free_bytes,"at":now});
                let _ = fs::write(&cache, row.to_string());
            }
            Ok(WindowsRam {
                free_bytes,
                source: "live",
            })
        }
        Err(why) => {
            let cached = fs::read_to_string(&cache)
                .ok()
                .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
                .and_then(|v| Some((v["free_bytes"].as_u64()?, v["at"].as_i64()?)))
                .filter(|(_, at)| *at <= now && now - *at <= max_age);
            cached
                .map(|(free_bytes, _)| WindowsRam {
                    free_bytes,
                    source: "cached",
                })
                .ok_or_else(|| {
                    format!("windows_ram_unreadable: {why}; no cached value within {max_age}s")
                })
        }
    }
}

/// Free RAM admission may use: WSL's view, capped by Windows free memory less its floor.
pub(super) fn effective_free(
    wsl_bytes: u64,
    windows: Option<WindowsRam>,
    windows_floor: u64,
) -> u64 {
    windows.map_or(wsl_bytes, |w| {
        wsl_bytes.min(w.free_bytes.saturating_sub(windows_floor))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    const GB: u64 = 1_000_000_000;

    #[test]
    fn wsl_is_told_from_the_kernel_string_or_the_interop_variable() {
        assert!(is_wsl("Linux version 6.18-microsoft-standard-WSL2", None));
        assert!(is_wsl(
            "Linux version 6.8 generic",
            Some("/run/WSL/1_interop")
        ));
        assert!(!is_wsl("Linux version 6.8.0-generic", None));
        assert!(!is_wsl("Linux version 6.8.0-generic", Some("")));
    }
    #[test]
    fn powershell_output_is_kib_and_anything_else_is_refused() {
        assert_eq!(parse_free_kb("18874368\r\n").unwrap(), 18_874_368 * 1024);
        assert!(parse_free_kb("").is_err());
        assert!(parse_free_kb("Get-CimInstance: error").is_err());
    }
    #[test]
    fn the_lower_of_wsl_and_windows_less_the_windows_floor_wins() {
        // The workstation of 2026-11-24: WSL says 85 GB, Windows has 19.2 GB free.
        let win = Some(WindowsRam {
            free_bytes: 19_200_000_000,
            source: "live",
        });
        assert_eq!(effective_free(85 * GB, win, 6 * GB), 13_200_000_000);
        // WSL lower than Windows: WSL wins. No Windows value (not WSL): WSL as is.
        assert_eq!(effective_free(5 * GB, win, 6 * GB), 5 * GB);
        assert_eq!(effective_free(85 * GB, None, 6 * GB), 85 * GB);
        // Windows below its floor admits nothing.
        let low = Some(WindowsRam {
            free_bytes: 4 * GB,
            source: "live",
        });
        assert_eq!(effective_free(85 * GB, low, 6 * GB), 0);
    }
    #[test]
    fn a_failed_read_uses_the_cache_for_one_window_then_refuses_with_the_reason() {
        let d = tempfile::tempdir().unwrap();
        let now = 1_800_000_000;
        let ok = read_with_cache(|| Ok(19 * GB), d.path(), now, 300, false).unwrap();
        assert_eq!(
            ok,
            WindowsRam {
                free_bytes: 19 * GB,
                source: "live"
            }
        );
        let calls = Cell::new(0);
        let fail = || {
            calls.set(calls.get() + 1);
            Err("powershell.exe timed out after 15s".to_string())
        };
        let cached = read_with_cache(fail, d.path(), now + 120, 300, false).unwrap();
        assert_eq!(
            cached,
            WindowsRam {
                free_bytes: 19 * GB,
                source: "cached"
            }
        );
        let err = read_with_cache(fail, d.path(), now + 301, 300, false).unwrap_err();
        assert!(
            err.starts_with("windows_ram_unreadable: powershell.exe timed out"),
            "{err}"
        );
        // No cache at all refuses too, and a dry run writes none.
        let empty = tempfile::tempdir().unwrap();
        assert!(read_with_cache(fail, empty.path(), now, 300, false).is_err());
        read_with_cache(|| Ok(GB), empty.path(), now, 300, true).unwrap();
        assert!(!empty.path().join(CACHE_FILE).exists());
    }
}
