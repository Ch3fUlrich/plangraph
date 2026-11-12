//! Child-side OOM policy: only async-signal-safe syscalls after fork.
use std::{ffi::CStr, io};

pub(super) const DENIED: &[u8] = b"{\"plangraph_oom\":{\"event\":\"oom_score_adj_denied\"}}\n";

pub(super) fn tolerate(error: io::Error, report: impl FnOnce(&[u8])) -> io::Result<()> {
    if matches!(error.raw_os_error(), Some(libc::EACCES | libc::EPERM)) {
        report(DENIED);
        Ok(())
    } else {
        report(b"{\"plangraph_oom\":{\"event\":\"oom_score_adj_failed\"}}\n");
        Ok(())
    }
}

pub(super) fn adjust(path: &CStr) -> io::Result<()> {
    // SAFETY: path and stack buffers remain valid for every syscall.
    unsafe {
        let report = |event: &[u8]| {
            libc::write(2, event.as_ptr().cast(), event.len());
        };
        let fd = libc::open(path.as_ptr(), libc::O_RDWR);
        if fd < 0 {
            return tolerate(io::Error::last_os_error(), report);
        }
        let result = (|| {
            let mut buf = [0u8; 32];
            let n = libc::read(fd, buf.as_mut_ptr().cast(), buf.len());
            if n < 0 {
                return Err(io::Error::last_os_error());
            }
            let score = std::str::from_utf8(&buf[..n as usize])
                .ok()
                .and_then(|s| s.trim().parse::<i32>().ok())
                .filter(|s| (-1000..=1000).contains(s))
                .ok_or_else(|| io::Error::from_raw_os_error(libc::EINVAL))?;
            if score >= 500 {
                return Ok(());
            }
            if libc::lseek(fd, 0, libc::SEEK_SET) < 0 {
                return Err(io::Error::last_os_error());
            }
            match libc::write(fd, b"500".as_ptr().cast(), 3) {
                3 => Ok(()),
                -1 => Err(io::Error::last_os_error()),
                _ => Err(io::Error::from_raw_os_error(libc::EIO)),
            }
        })();
        libc::close(fd);
        result.or_else(|error| tolerate(error, report))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn denial_is_recorded_in_runner_envelope() {
        for errno in [libc::EACCES, libc::EPERM] {
            let mut event = Vec::new();
            tolerate(io::Error::from_raw_os_error(errno), |s| event.extend(s)).unwrap();
            let value: serde_json::Value = serde_json::from_slice(&event).unwrap();
            assert_eq!(value["plangraph_oom"]["event"], "oom_score_adj_denied");
        }
    }
    #[test]
    fn other_errors_are_reported_without_blocking_spawn() {
        for errno in [libc::EINVAL, libc::EIO, libc::ENOENT] {
            let mut event = Vec::new();
            tolerate(io::Error::from_raw_os_error(errno), |s| event.extend(s)).unwrap();
            let value: serde_json::Value = serde_json::from_slice(&event).unwrap();
            assert_eq!(value["plangraph_oom"]["event"], "oom_score_adj_failed");
        }
    }
    #[test]
    fn injected_scores_never_lower_or_write_unknown_values() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("score");
        let cpath = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        for (before, after) in [("100", "500"), ("800", "800"), ("", ""), ("bad", "bad")] {
            std::fs::write(&path, before).unwrap();
            assert!(adjust(&cpath).is_ok());
            assert_eq!(std::fs::read_to_string(&path).unwrap(), after);
        }
    }
}
