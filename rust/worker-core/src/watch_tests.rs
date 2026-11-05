use super::*;

#[derive(Default)]
struct Fake {
    terms: u32,
    kills: u32,
    reaps: u32,
    empty: bool,
    denied: bool,
    signals_fail: bool,
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
        if self.denied {
            Err(io::Error::from_raw_os_error(libc::EPERM))
        } else {
            Ok(self.empty)
        }
    }
    fn reap(&mut self) -> io::Result<()> {
        self.reaps += 1;
        Ok(())
    }
}

#[test]
fn single_kill_path_terminates_then_kills_once() {
    let (mut stop, mut child) = (Stop::default(), Fake::default());
    for now in 0..3000 {
        assert_eq!(stop.tick(&mut child, now, 1000), None);
    }
    assert_eq!((child.terms, child.kills), (1, 1));
    child.empty = true;
    assert_eq!(stop.tick(&mut child, 3000, 1000), Some(KillProof::Empty));
    assert!(child.reaps > 0);
}

#[test]
fn surviving_group_is_kill_incomplete() {
    let (mut stop, mut child) = (Stop::default(), Fake::default());
    assert_eq!(stop.tick(&mut child, 0, 10), None);
    assert_eq!(stop.tick(&mut child, 10, 10), None);
    assert_eq!(
        stop.tick(&mut child, 2010, 10),
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
    assert_eq!(stop.tick(&mut child, 0, 0), None);
    assert_eq!(
        stop.tick(&mut child, 2000, 0),
        Some(KillProof::Survivors {
            cause: Some(io::ErrorKind::PermissionDenied)
        })
    );
}

#[test]
fn foreign_group_never_signalled() {
    for pgid in [-1, 0, 1, unsafe { libc::getpgrp() }] {
        assert_eq!(
            crate::stall::group_empty(pgid).unwrap_err().kind(),
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
    assert_eq!(stop.tick(&mut child, 0, 0), None);
    assert_eq!(
        stop.tick(&mut child, 2000, 0),
        Some(KillProof::Survivors {
            cause: Some(io::ErrorKind::PermissionDenied)
        })
    );
    assert_eq!((child.terms, child.kills), (1, 1));
}
