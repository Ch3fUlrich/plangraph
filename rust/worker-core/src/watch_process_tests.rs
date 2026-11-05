use super::*;

#[test]
fn grandchild_group_killed_even_after_leader_exit() {
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
    child.kill().unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    // A zombie may remain until the host's init reaps it: verify no live member.
    loop {
        let live = std::fs::read_dir("/proc").unwrap().flatten().any(|entry| {
            let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
                return false;
            };
            let Some((_, fields)) = stat.rsplit_once(") ") else {
                return false;
            };
            let fields: Vec<_> = fields.split_whitespace().collect();
            fields.get(2) == Some(&child.pgid.to_string().as_str()) && fields[0] != "Z"
        });
        if !live {
            break;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
}

#[test]
fn watch_esrch_latches_and_never_signals_again() {
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "exit 0"]);
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    child.child.wait().unwrap();
    assert!(child.empty().unwrap());
    assert!(child.gone);
    child.pgid = unsafe { libc::getpgrp() };
    assert!(child.empty().unwrap());
    child.kill().unwrap();
}
