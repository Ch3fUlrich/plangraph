//! Supervise a child process: own process group, stall watchdog, overall timeout.
//!
//! stdout and stderr share one pipe, so any output on either counts as activity.
//! On stall or timeout the whole group gets SIGTERM, then SIGKILL after a grace.

use std::io::{self, Read};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct Opts {
    /// Kill when no output appears for this long.
    pub stall: Option<Duration>,
    /// Kill when the child runs this long overall.
    pub timeout: Option<Duration>,
    /// Delay between SIGTERM and SIGKILL.
    pub grace: Duration,
    /// Keep at most this many trailing bytes of combined output.
    pub max_output: usize,
}

impl Default for Opts {
    fn default() -> Self {
        Opts {
            stall: None,
            timeout: None,
            grace: Duration::from_secs(10),
            max_output: 1 << 20,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Supervised {
    pub code: Option<i32>,
    pub signal: Option<i32>,
    pub stalled: bool,
    pub timed_out: bool,
    /// The caller's cancel closure fired, so we terminated the group early.
    pub cancelled: bool,
    /// Trailing combined stdout+stderr.
    pub output: Vec<u8>,
}

fn killpg(pgid: i32, sig: i32) {
    // SAFETY: plain syscall; errors (e.g. ESRCH when the group is gone) are ignored.
    unsafe {
        libc::killpg(pgid, sig);
    }
}

fn read_tail(mut r: impl Read, start: Instant, last: &AtomicU64, max: usize) -> Vec<u8> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    while let Ok(n) = r.read(&mut chunk) {
        if n == 0 {
            break;
        }
        last.store(start.elapsed().as_millis() as u64, Ordering::Relaxed);
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > max.saturating_mul(2) {
            buf.drain(..buf.len() - max);
        }
    }
    if buf.len() > max {
        buf.drain(..buf.len() - max);
    }
    buf
}

/// Run `cmd` to completion under the watchdog described in the module docs.
///
/// Thin wrapper for callers with no cancellation source.
pub fn supervise(cmd: Command, opts: &Opts) -> io::Result<Supervised> {
    supervise_with_cancel(cmd, opts, &|| false)
}

/// Like [`supervise`], but also stops the child when `cancelled` reports true.
///
/// The cancel check is polled alongside stall/timeout and reuses the same
/// SIGTERM-then-SIGKILL-after-grace path, so a cancelling caller never leaves a
/// supervised child running.
pub fn supervise_with_cancel(
    mut cmd: Command,
    opts: &Opts,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<Supervised> {
    let (reader, writer) = io::pipe()?;
    cmd.stdin(Stdio::null())
        .stdout(writer.try_clone()?)
        .stderr(writer)
        .process_group(0);
    let mut child = cmd.spawn()?;
    drop(cmd); // closes our copies of the write end so the reader sees EOF
    let pgid = child.id() as i32;
    let start = Instant::now();
    let last = Arc::new(AtomicU64::new(0));
    let (last2, max) = (last.clone(), opts.max_output);
    let reader = thread::spawn(move || read_tail(reader, start, &last2, max));

    let (mut stalled, mut timed_out, mut cancelled_flag) = (false, false, false);
    let mut term_at: Option<Instant> = None;
    let mut killed = false;
    let status = loop {
        if let Some(s) = child.try_wait()? {
            break s;
        }
        let now = Instant::now();
        match term_at {
            None => {
                let quiet = now.saturating_duration_since(
                    start + Duration::from_millis(last.load(Ordering::Relaxed)),
                );
                if opts.timeout.is_some_and(|t| now - start >= t) {
                    timed_out = true;
                } else if opts.stall.is_some_and(|s| quiet >= s) {
                    stalled = true;
                } else if cancelled() {
                    cancelled_flag = true;
                }
                if timed_out || stalled || cancelled_flag {
                    killpg(pgid, libc::SIGTERM);
                    term_at = Some(now);
                }
            }
            Some(t) if !killed && now - t >= opts.grace => {
                killpg(pgid, libc::SIGKILL);
                killed = true;
            }
            Some(_) => {}
        }
        thread::sleep(Duration::from_millis(10));
    };
    // Leave nothing behind (orphans would also keep the pipe open).
    killpg(pgid, libc::SIGKILL);
    let output = reader.join().unwrap_or_default();
    Ok(Supervised {
        code: status.code(),
        signal: status.signal(),
        stalled,
        timed_out,
        cancelled: cancelled_flag,
        output,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    fn sh(script: &str) -> Command {
        let mut c = Command::new("sh");
        c.arg("-c").arg(script);
        c
    }

    fn quick() -> Opts {
        Opts {
            grace: Duration::from_millis(300),
            ..Opts::default()
        }
    }

    #[test]
    fn normal_exit_captures_both_streams() {
        let r = supervise(sh("echo out; echo err >&2; exit 3"), &quick()).unwrap();
        assert_eq!(
            (r.code, r.signal, r.stalled, r.timed_out),
            (Some(3), None, false, false)
        );
        let s = String::from_utf8(r.output).unwrap();
        assert!(s.contains("out") && s.contains("err"));
    }

    #[test]
    fn silent_child_is_stalled_and_termed() {
        let o = Opts {
            stall: Some(Duration::from_millis(300)),
            ..quick()
        };
        let t = Instant::now();
        let r = supervise(sh("sleep 30"), &o).unwrap();
        assert!(r.stalled && !r.timed_out);
        assert_eq!(r.signal, Some(libc::SIGTERM));
        assert!(t.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn output_resets_stall_window() {
        let o = Opts {
            stall: Some(Duration::from_millis(600)),
            ..quick()
        };
        let r = supervise(sh("for i in 1 2 3 4 5; do echo tick; sleep 0.25; done"), &o).unwrap();
        assert!(!r.stalled);
        assert_eq!(r.code, Some(0));
    }

    #[test]
    fn overall_timeout_even_with_output() {
        let o = Opts {
            timeout: Some(Duration::from_millis(400)),
            stall: Some(Duration::from_secs(60)),
            ..quick()
        };
        let r = supervise(sh("while :; do echo x; sleep 0.05; done"), &o).unwrap();
        assert!(r.timed_out && !r.stalled);
        assert_eq!(r.signal, Some(libc::SIGTERM));
    }

    #[test]
    fn sigterm_ignorer_is_sigkilled_after_grace() {
        let o = Opts {
            timeout: Some(Duration::from_millis(200)),
            ..quick()
        };
        let t = Instant::now();
        let r = supervise(sh("trap '' TERM; echo up; sleep 30"), &o).unwrap();
        assert!(r.timed_out);
        assert_eq!(r.signal, Some(libc::SIGKILL));
        assert!(t.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn whole_group_dies_and_pipe_closes() {
        // A background grandchild holds the pipe; the group kill must free us.
        let t = Instant::now();
        let r = supervise(sh("sleep 30 & echo done"), &quick()).unwrap();
        assert_eq!(r.code, Some(0));
        assert!(t.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn tail_keeps_exactly_the_last_max_bytes() {
        for max in [0usize, 1, 7] {
            let o = Opts {
                max_output: max,
                ..quick()
            };
            let r = supervise(
                sh("printf 0123456789abcdefghij; sleep 0.1; printf KLMNOPQRST"),
                &o,
            )
            .unwrap();
            let all = b"0123456789abcdefghijKLMNOPQRST";
            assert_eq!(r.output, all[all.len() - max..], "max={max}");
        }
    }

    #[test]
    fn output_is_truncated_to_tail() {
        let o = Opts {
            max_output: 100,
            ..quick()
        };
        let r = supervise(sh("seq 1 5000"), &o).unwrap();
        assert!(r.output.len() <= 100);
        assert!(String::from_utf8_lossy(&r.output)
            .trim_end()
            .ends_with("5000"));
    }

    #[test]
    fn cancel_terms_a_running_child() {
        let flag = Arc::new(AtomicBool::new(false));
        let f = flag.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            f.store(true, Ordering::Relaxed);
        });
        let t = Instant::now();
        let r = supervise_with_cancel(sh("sleep 600"), &quick(), &|| flag.load(Ordering::Relaxed))
            .unwrap();
        assert!(t.elapsed() < Duration::from_secs(5));
        assert!(r.cancelled && !r.stalled && !r.timed_out);
        assert_eq!(r.signal, Some(libc::SIGTERM));
    }

    #[test]
    fn cancel_sigkills_a_sigterm_ignorer_after_grace() {
        let flag = Arc::new(AtomicBool::new(false));
        let f = flag.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            f.store(true, Ordering::Relaxed);
        });
        let t = Instant::now();
        let r = supervise_with_cancel(sh("trap '' TERM; echo up; sleep 600"), &quick(), &|| {
            flag.load(Ordering::Relaxed)
        })
        .unwrap();
        assert!(r.cancelled && !r.stalled && !r.timed_out);
        assert_eq!(r.signal, Some(libc::SIGKILL));
        assert!(t.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn normal_exit_is_not_cancelled() {
        let r = supervise_with_cancel(sh("exit 0"), &quick(), &|| false).unwrap();
        assert!(!r.cancelled && !r.stalled && !r.timed_out);
        assert_eq!(r.code, Some(0));
    }
}
