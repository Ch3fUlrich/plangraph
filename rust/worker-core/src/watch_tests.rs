use super::*;
use std::os::unix::process::ExitStatusExt;

#[test]
fn review_layout_contract() {
    let stall = include_str!("stall/unix.rs");
    assert!(!stall.contains("rustfmt::skip"));
    assert!(!stall.contains("fn group_empty"));
    assert_eq!(
        include_str!("../../../changelog.d/SPAWNER-RL12-B1.md")
            .lines()
            .filter(|line| line.starts_with("- "))
            .count(),
        1
    );
    let process = include_str!("watch_process_tests.rs");
    assert_eq!(process.matches("let deadline =").count(), 2);
    assert!(process.contains("read_dir(\"/proc\")"));
    assert!(!include_str!("watch_unix.rs").contains("PROBES"));
}

#[derive(Default)]
struct Fake {
    terms: u32,
    kills: u32,
    reaps: u32,
    empty: bool,
    denied: bool,
    signals_fail: bool,
    deny_once: bool,
    probes: u32,
    exited: bool,
}
impl Child for Fake {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.terms += 1;
        if self.signals_fail {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        Ok(())
    }
    fn kill(&mut self) -> io::Result<()> {
        self.kills += 1;
        if self.signals_fail {
            return Err(io::ErrorKind::BrokenPipe.into());
        }
        Ok(())
    }
    fn empty(&mut self) -> io::Result<bool> {
        self.probes += 1;
        if std::mem::take(&mut self.deny_once) || self.denied {
            Err(io::Error::from_raw_os_error(libc::EPERM))
        } else {
            Ok(self.empty)
        }
    }
    fn reap(&mut self) -> io::Result<Option<ExitStatus>> {
        self.reaps += 1;
        Ok(self.exited.then(|| ExitStatus::from_raw(0)))
    }
}

#[test]
fn exited_root_probes_only_on_edge_and_deadlines() {
    let (mut stop, mut child) = (Stop::default(), Fake::default());
    stop.tick(&mut child, 0, 1000, 2000);
    child.exited = true;
    for now in 1..3000 {
        assert_eq!(stop.tick(&mut child, now, 1000, 2000), None);
    }
    assert_eq!(child.probes, 3); // initial, exit edge, grace
    assert_eq!(
        stop.tick(&mut child, 3000, 1000, 2000),
        Some(KillProof::Survivors { cause: None })
    );
    assert_eq!(child.probes, 4);
}

#[test]
fn first_probe_error_is_reported_at_each_signal() {
    for kill_probe in [false, true] {
        let (mut stop, mut child) = (Stop::default(), Fake::default());
        child.deny_once = !kill_probe;
        stop.tick(&mut child, 0, 10, 2000);
        child.deny_once = kill_probe;
        stop.tick(&mut child, 10, 10, 2000);
        assert_eq!(
            stop.tick(&mut child, 2010, 10, 2000),
            Some(KillProof::Survivors {
                cause: Some(io::ErrorKind::PermissionDenied)
            })
        );
    }
}

#[test]
fn single_kill_path_terminates_then_kills_once() {
    let (mut stop, mut child) = (Stop::default(), Fake::default());
    for now in 0..3000 {
        assert_eq!(stop.tick(&mut child, now, 1000, 2000), None);
    }
    assert_eq!((child.terms, child.kills), (1, 1));
    child.empty = true;
    assert_eq!(
        stop.tick(&mut child, 3000, 1000, 2000),
        Some(KillProof::Empty)
    );
    assert!(child.reaps > 0);
}

#[test]
fn surviving_group_is_kill_incomplete() {
    let (mut stop, mut child) = (Stop::default(), Fake::default());
    assert_eq!(stop.tick(&mut child, 0, 10, 2000), None);
    assert_eq!(stop.tick(&mut child, 10, 10, 2000), None);
    assert_eq!(
        stop.tick(&mut child, 2010, 10, 2000),
        Some(KillProof::Survivors { cause: None })
    );
}

#[test]
fn esrch_empty_eperm_not_proof() {
    owned_child_is_reaped_and_empty(); // Real ESRCH probe, not a fake boolean.
    let mut child = Fake {
        denied: true,
        empty: true,
        ..Fake::default()
    };
    let mut stop = Stop::default();
    assert_eq!(stop.tick(&mut child, 0, 0, 2000), None);
    assert_eq!(
        stop.tick(&mut child, 2000, 0, 2000),
        Some(KillProof::Survivors {
            cause: Some(io::ErrorKind::PermissionDenied)
        })
    );
}

#[test]
fn foreign_group_never_signalled() {
    for pgid in [-1, 0, 1, unsafe { libc::getpgrp() }] {
        assert_eq!(
            super::unix::group_empty(pgid).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }
}

#[test]
fn owned_child_is_reaped_and_empty() {
    let mut cmd = std::process::Command::new("sh");
    cmd.args(["-c", "exit 0"]);
    let mut child = unix::UnixChild::spawn(&mut cmd).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while child.try_wait().unwrap().is_none() {
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
    child.reap().unwrap();
    assert!(child.empty().unwrap());
}

#[test]
fn first_signal_error_is_reported() {
    let mut child = Fake {
        signals_fail: true,
        ..Fake::default()
    };
    let mut stop = Stop::default();
    assert_eq!(stop.tick(&mut child, 0, 0, 2000), None);
    assert_eq!(
        stop.tick(&mut child, 2000, 0, 2000),
        Some(KillProof::Survivors {
            cause: Some(io::ErrorKind::PermissionDenied)
        })
    );
    assert_eq!((child.terms, child.kills), (1, 1));
}
