use super::*;

#[test]
fn watch_reaped_leader_with_live_descendant_refuses_signals() {
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "sleep 30 & exit 0"]);
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    // Always clean up our known group, including when an assertion unwinds.
    struct Cleanup(i32);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = killpg(self.0, libc::SIGKILL);
        }
    }
    let _cleanup = Cleanup(child.pgid);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while child.try_wait().unwrap().is_none() {
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert!(!child.empty().unwrap());
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        assert_eq!(
            child.signal(signal).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }
    let mut stop = crate::watch::Stop::default();
    assert_eq!(stop.tick(&mut child, 0, 0), None);
    assert_eq!(
        stop.tick(&mut child, 2000, 0),
        Some(crate::watch::KillProof::Survivors)
    );
}

#[cfg(target_os = "linux")]
#[test]
fn watch_missing_captured_identity_refuses_live_leader() {
    let mut cmd = Command::new("sleep");
    cmd.arg("30");
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    child.start = None;
    let result = child.signal(libc::SIGTERM);
    let _ = child.child.kill();
    let _ = child.child.wait();
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
}
