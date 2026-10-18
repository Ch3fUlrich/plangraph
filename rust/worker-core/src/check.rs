//! Run a card's check command and keep the tail of its output.

use crate::hash::hash_bytes;
use crate::redact;
use crate::stall::{supervise_with_cancel, Opts};
use serde::{Deserialize, Serialize};
use std::io;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckResult {
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub timed_out: bool,
    /// The caller's cancel closure fired; the process group was killed.
    #[serde(default)]
    pub cancelled: bool,
    /// Last `tail_n` lines of combined stdout+stderr, redacted as a WHOLE
    /// before the line cut and byte-capped (see `redact::redacted_tail`).
    pub tail: String,
    /// Hash of `tail`, for "identical failure" comparisons.
    pub output_hash: String,
}

impl CheckResult {
    pub fn passed(&self) -> bool {
        self.exit_code == Some(0) && !self.timed_out && !self.cancelled
    }
}

/// Last `n` lines of `text`.
pub fn tail_lines(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

/// Run `cmd` via `sh -c` in `dir`; kill it after `timeout`; let `cancelled`
/// stop the whole process group early.
///
/// The child gets the secret-free environment of [`crate::sandbox_env`]
/// (no agent credentials, unlike the harness) and its own private `HOME` (`<dir>.home-check`, never the harness HOME), never the worker's own environment.
///
/// The tail is built from the WHOLE supervised output, redacted FIRST and cut
/// to the last `tail_n` lines afterwards: cutting first would let a multi-line
/// secret (a PEM block) straddle the cut and leak the half behind it.
pub fn run_check_cancellable(
    cmd: &str,
    dir: &Path,
    timeout: Duration,
    tail_n: usize,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<CheckResult> {
    let home = crate::sandbox_env::HomeGuard::for_check(dir)?;
    let mut c = Command::new("sh");
    c.arg("-c").arg(cmd).current_dir(dir);
    crate::sandbox_env::confine_check(&mut c, home.path());
    let opts = Opts {
        timeout: Some(timeout),
        grace: Duration::from_secs(2),
        ..Opts::default()
    };
    let r = supervise_with_cancel(c, &opts, cancelled)?;
    let tail = redact::redacted_tail(
        &String::from_utf8_lossy(&r.output),
        tail_n,
        redact::MAX_TAIL_BYTES,
    );
    Ok(CheckResult {
        exit_code: r.code,
        signal: r.signal,
        timed_out: r.timed_out,
        cancelled: r.cancelled,
        output_hash: hash_bytes(tail.as_bytes()),
        tail,
    })
}

/// Non-cancellable wrapper around [`run_check_cancellable`].
///
/// The child gets the secret-free environment of [`crate::sandbox_env`]
/// (no agent credentials, unlike the harness) and its own private `HOME` (`<dir>.home-check`, never the harness HOME), never the worker's own environment.
/// The tail is redacted before it is cut, exactly as in
/// [`run_check_cancellable`]: merge-time, probe and attempt callers all report
/// through history too.
pub fn run_check(
    cmd: &str,
    dir: &Path,
    timeout: Duration,
    tail_n: usize,
) -> io::Result<CheckResult> {
    run_check_cancellable(cmd, dir, timeout, tail_n, &|| false)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::time::Instant;

    const T: Duration = Duration::from_secs(20);

    #[test]
    fn pass_and_fail_codes() {
        let d = tempfile::tempdir().unwrap();
        let ok = run_check("echo fine", d.path(), T, 50).unwrap();
        assert!(ok.passed());
        assert_eq!(ok.tail, "fine");
        let bad = run_check("echo nope; exit 7", d.path(), T, 50).unwrap();
        assert!(!bad.passed());
        assert_eq!(bad.exit_code, Some(7));
    }

    #[test]
    fn runs_in_dir_and_combines_streams() {
        let d = tempfile::tempdir().unwrap();
        std::fs::write(d.path().join("marker"), "").unwrap();
        let r = run_check("ls marker; echo e >&2", d.path(), T, 50).unwrap();
        assert!(r.tail.contains("marker") && r.tail.contains('e'));
    }

    #[test]
    #[serial_test::serial(env)]
    fn check_runs_without_worker_secrets_and_with_a_private_home() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("c1-1");
        std::fs::create_dir(&dir).unwrap();
        // A login the harness persisted in its own HOME must be invisible here.
        let harness_home = root.path().join("c1-1.home");
        std::fs::create_dir(&harness_home).unwrap();
        std::fs::write(harness_home.join("harness-login"), "t").unwrap();
        // Unique to this test: no other test reads or sets it.
        std::env::set_var("PG_SENTINEL_SECRET_CHECK", "hunter2");
        let r = run_check("env; ls -a \"$HOME\"; echo done", &dir, T, 500).unwrap();
        assert!(r.passed());
        assert!(!r.tail.contains("harness-login"), "{}", r.tail);
        assert!(
            !r.tail.contains("SENTINEL") && !r.tail.contains("hunter2"),
            "{}",
            r.tail
        );
        let home = root.path().join("c1-1.home-check");
        assert!(
            r.tail.contains(&format!("HOME={}", home.display())),
            "{}",
            r.tail
        );
        // The private home is removed once the check is done.
        assert!(!home.exists());
    }

    #[test]
    fn keeps_only_last_n_lines() {
        let d = tempfile::tempdir().unwrap();
        let r = run_check("seq 1 200", d.path(), T, 50).unwrap();
        let lines: Vec<&str> = r.tail.lines().collect();
        assert_eq!(lines.len(), 50);
        assert_eq!(lines[0], "151");
        assert_eq!(lines[49], "200");
        assert_eq!(tail_lines("a\nb", 5), "a\nb");
    }

    /// A `sh` script printing `lines`, one per line. Line content must not
    /// contain single quotes.
    fn prints(lines: &[String]) -> String {
        lines
            .iter()
            .map(|l| format!("printf '%s\\n' '{l}'"))
            .collect::<Vec<_>>()
            .join("; ")
    }

    /// The PEM block of the tests below: an obviously fake header,
    /// letter-only base64 bodies — which the generic blob pattern would NOT
    /// catch (it needs digits and letters), so the PEM pattern is what must —
    /// and the matching footer. `80` output lines with a cut at the last 50
    /// puts the cut at line 31.
    fn pem_block(body_lines: usize) -> Vec<String> {
        let mut v = vec!["-----BEGIN TEST PRIVATE KEY-----".to_string()];
        for _ in 0..body_lines {
            v.push("A".repeat(64));
        }
        v.push("-----END TEST PRIVATE KEY-----".to_string());
        v
    }

    fn assert_tail_has_no_key(tail: &str) {
        assert!(!tail.contains("PRIVATE KEY"), "{tail}");
        assert!(!tail.contains("BEGIN") && !tail.contains("END"), "{tail}");
        assert!(!tail.contains(&"A".repeat(64)), "{tail}");
        assert!(tail.contains(redact::REDACTED), "{tail}");
    }

    #[test]
    fn pem_block_straddling_the_cut_leaves_no_part_of_the_key() {
        // 80 lines; the PEM block sits at lines 20..41 (header, 20 body lines,
        // footer). The old order — cut to the last 50 lines, then redact the
        // cut — kept the footer (line 41) and every body line past line 31 in
        // the tail, so this test fails on it.
        let block = pem_block(20);
        let mut lines: Vec<String> = (1..=19).map(|i| format!("filler {i}")).collect();
        lines.extend(block.iter().cloned());
        lines.extend((42..=80).map(|i| format!("filler {i}")));
        let d = tempfile::tempdir().unwrap();
        let r = run_check(&prints(&lines), d.path(), T, 50).unwrap();
        assert_tail_has_no_key(&r.tail);
        assert_eq!(r.output_hash, hash_bytes(r.tail.as_bytes()));
        assert_eq!(r.tail.lines().count(), 50);
        assert!(r.tail.contains("filler 80"), "{}", r.tail);
    }

    #[test]
    fn pem_header_before_the_cut_and_body_after_it_is_redacted() {
        // The header sits at line 25, before the cut (the tail keeps lines
        // 31..80), and the body after it: cutting first leaves raw body lines
        // with no header to tell redaction what they are. Redacting the WHOLE
        // output first is the fix this test pins.
        let block = pem_block(30);
        let mut lines: Vec<String> = (1..=24).map(|i| format!("filler {i}")).collect();
        lines.extend(block.iter().cloned());
        lines.extend((57..=80).map(|i| format!("filler {i}")));
        let d = tempfile::tempdir().unwrap();
        let r = run_check(&prints(&lines), d.path(), T, 50).unwrap();
        assert_tail_has_no_key(&r.tail);
        assert!(r.tail.lines().count() <= 50);
        assert!(r.tail.contains("filler 80"), "{}", r.tail);
    }

    #[test]
    fn timeout_is_reported() {
        let d = tempfile::tempdir().unwrap();
        let r = run_check(
            "echo start; sleep 30",
            d.path(),
            Duration::from_millis(300),
            50,
        )
        .unwrap();
        assert!(r.timed_out && !r.passed());
        assert!(r.tail.contains("start"));
    }

    #[test]
    fn same_output_same_hash() {
        let d = tempfile::tempdir().unwrap();
        let a = run_check("echo boom; exit 1", d.path(), T, 50).unwrap();
        let b = run_check("echo boom; exit 1", d.path(), T, 50).unwrap();
        let c = run_check("echo other; exit 1", d.path(), T, 50).unwrap();
        assert_eq!(a.output_hash, b.output_hash);
        assert_ne!(a.output_hash, c.output_hash);
    }

    /// True if Linux still exposes this pid as a live (non-zombie) process.
    fn pid_alive(pid: &str) -> bool {
        std::fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|s| {
            s.rsplit(')')
                .next()
                .and_then(|t| t.split_whitespace().next())
                != Some("Z")
        })
    }

    #[test]
    fn cancel_kills_the_child_and_its_group() {
        let d = tempfile::tempdir().unwrap();
        let dir = d.path().to_path_buf();
        let pid_file = dir.join("child.pid");
        let flag = Arc::new(AtomicBool::new(false));
        let start = Instant::now();
        let handle = {
            let pf = pid_file.clone();
            let flag = Arc::clone(&flag);
            std::thread::spawn(move || {
                // Wait (up to 5 s) until the command recorded the grandchild
                // pid, then cancel ~300 ms later.
                for _ in 0..500 {
                    if pf.exists() {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                std::thread::sleep(Duration::from_millis(300));
                flag.store(true, Ordering::SeqCst);
            })
        };
        let r = run_check_cancellable(
            "sleep 60 & echo $! > child.pid; wait",
            &dir,
            Duration::from_secs(30),
            50,
            &|| flag.load(Ordering::SeqCst),
        )
        .unwrap();
        handle.join().unwrap();
        assert!(r.cancelled);
        assert!(!r.passed());
        assert!(!r.timed_out);
        assert!(start.elapsed() < Duration::from_secs(15));
        let pid = std::fs::read_to_string(&pid_file).unwrap();
        let pid = pid.trim();
        if Path::new("/proc/self").exists() {
            let mut dead = false;
            for _ in 0..300 {
                if !pid_alive(pid) {
                    dead = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            assert!(dead, "backgrounded sleep {pid} is still alive");
        }
    }

    #[test]
    fn already_cancelled_never_runs_to_completion() {
        let d = tempfile::tempdir().unwrap();
        let start = Instant::now();
        let r = run_check_cancellable("sleep 30", d.path(), Duration::from_secs(30), 50, &|| true)
            .unwrap();
        assert!(r.cancelled);
        assert!(start.elapsed() < Duration::from_secs(15));
    }

    #[test]
    fn not_cancelled_is_unchanged() {
        let d = tempfile::tempdir().unwrap();
        let r = run_check_cancellable("echo hi", d.path(), T, 50, &|| false).unwrap();
        assert!(r.passed());
        assert!(!r.cancelled);
        assert_eq!(r.tail, "hi");
    }

    #[test]
    fn serde_default_for_cancelled() {
        let v = serde_json::json!({
            "exit_code": 0,
            "signal": null,
            "timed_out": false,
            "tail": "",
            "output_hash": "",
        });
        let r: CheckResult = serde_json::from_value(v).unwrap();
        assert!(!r.cancelled);
        assert!(r.passed());
    }
}
