//! Local host maintenance commands.
use std::{
    fs,
    io::{self, Write},
    path::Path,
    process::Command,
    time::Duration,
};

const WSL_GUARD_USAGE: &str = "Usage: pg-spawnd host wsl-cache-guard [--min-free-mb N]";
const DEFAULT_MIN_FREE_MB: u64 = 20_480;

pub(crate) fn execute(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let args: Vec<_> = args.iter().map(String::as_str).collect();
    let line = match args.as_slice() {
        ["work-disk"] => {
            let (root, _) = pg_worker_core::work_disks::worktree_disk()
                .map_err(|error| error.to_string())?
                .ok_or("disk_low: no worktree mounts configured")?;
            let path = root.to_str().ok_or("worktree root is not UTF-8")?;
            serde_json::to_string(path).map_err(|error| error.to_string())?
        }
        ["wsl-cache-guard", "--help" | "-h"] => WSL_GUARD_USAGE.into(),
        ["wsl-cache-guard"] => wsl_guard(DEFAULT_MIN_FREE_MB)?,
        ["wsl-cache-guard", "--min-free-mb", value] => {
            let min = value
                .parse::<u64>()
                .ok()
                .filter(|&min| min > 0)
                .ok_or("--min-free-mb requires a positive integer")?;
            wsl_guard(min)?
        }
        ["wsl-cache-guard", ..] => return Err(WSL_GUARD_USAGE.into()),
        _ => return Err("Usage: pg-spawnd host <work-disk|wsl-cache-guard>".into()),
    };
    writeln!(out, "{line}").map_err(|error| error.to_string())
}

fn windows_free_mb() -> io::Result<u64> {
    let powershell =
        Path::new("/mnt/c/Windows/System32").join("WindowsPowerShell/v1.0/powershell.exe");
    let bytes = pg_worker_core::host_command::output(
        Command::new(powershell).args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).FreePhysicalMemory",
        ]),
        Duration::from_secs(20),
    )?;
    let kb = std::str::from_utf8(&bytes)
        .map_err(io::Error::other)?
        .trim()
        .parse::<u64>()
        .map_err(io::Error::other)?;
    Ok(kb / 1024)
}

fn drop_page_cache() -> io::Result<()> {
    pg_worker_core::host_command::output(
        &mut Command::new("/usr/bin/sync"),
        Duration::from_secs(60),
    )?;
    fs::OpenOptions::new()
        .write(true)
        .open("/proc/sys/vm/drop_caches")?
        .write_all(b"1\n")
}

fn wsl_guard(min: u64) -> Result<String, String> {
    let release = fs::read_to_string("/proc/sys/kernel/osrelease").unwrap_or_default();
    if !pg_worker_core::wsl::is_wsl(&release) {
        return Ok("wsl-cache-guard status=skipped reason=not-wsl".into());
    }
    reclaim(min, windows_free_mb, drop_page_cache)
        .map_err(|error| format!("wsl-cache-guard drop-caches failed: {error}"))
}

fn reclaim(
    min: u64,
    mut memory: impl FnMut() -> io::Result<u64>,
    drop: impl FnOnce() -> io::Result<()>,
) -> io::Result<String> {
    let before = match memory() {
        Ok(before) => before,
        Err(error) => {
            return Ok(format!(
                "wsl-cache-guard status=skipped reason=interop-unavailable error={error}"
            ));
        }
    };
    if before >= min {
        return Ok(format!(
            "wsl-cache-guard status=healthy before_free_mb={before} min_free_mb={min}"
        ));
    }
    drop().map_err(|error| io::Error::other(format!("before_free_mb={before}: {error}")))?;
    let after = match memory() {
        Ok(after) => format!("after_free_mb={after}"),
        Err(error) => format!("after_free_mb=unknown reason=interop-unavailable error={error}"),
    };
    Ok(format!("wsl-cache-guard status=dropped-page-cache before_free_mb={before} min_free_mb={min} {after}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_guard_skips_failed_interop_and_honors_the_floor() {
        let never = || panic!("page cache should not be dropped");
        let result = reclaim(20, || Err(io::Error::other("fixture unavailable")), never).unwrap();
        assert!(result.contains("reason=interop-unavailable"), "{result}");
        let result = reclaim(20, || Ok(20), never).unwrap();
        assert!(result.contains("status=healthy"), "{result}");
    }

    #[test]
    fn cache_guard_resamples_after_dropping_only_below_the_floor() {
        let mut samples = [19, 32].into_iter();
        let called = std::cell::Cell::new(false);
        let result = reclaim(
            20,
            || {
                let sample = samples.next().unwrap();
                assert_eq!(called.get(), sample == 32);
                Ok(sample)
            },
            || {
                called.set(true);
                Ok(())
            },
        )
        .unwrap();
        assert!(
            result.contains("before_free_mb=19 min_free_mb=20 after_free_mb=32"),
            "{result}"
        );
    }
}
