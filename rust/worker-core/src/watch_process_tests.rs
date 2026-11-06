use super::*;

#[test]
fn foundation_docs_exclude_followup_slice_narrative() {
    let docs = include_str!("../../../docs/spawner-reliability.md");
    let foundation = docs.split("The RL12 B1 foundation").nth(1).unwrap();
    let foundation = foundation.split("The remaining behavior").next().unwrap();
    assert!(!foundation.contains("Stop retains"));
    assert!(!foundation.contains("stdin writer is retained"));
    assert!(foundation.contains("Watched.io_error"));
    assert!(foundation.contains("`killpg` rejects"));
}

#[test]
fn foundation_dead_code_allowances_are_production_only() {
    for source in [include_str!("watch_unix.rs"), include_str!("watch.rs")] {
        for line in source
            .lines()
            .filter(|line| line.contains("allow(dead_code)"))
        {
            assert!(line.starts_with("#[cfg_attr(not(test), allow(dead_code))]"));
        }
    }
}

#[test]
fn stdin_writer_cancels_when_child_does_not_read() {
    let mut cmd = Command::new("sleep");
    cmd.arg("30").stdin(std::process::Stdio::piped());
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    let writer = stdin_writer(child.child.stdin.take().unwrap(), &vec![0; 1024 * 1024]);
    let started = std::time::Instant::now();
    let result = writer.unwrap().finish();
    let cleanup = child.kill();
    child.child.wait().unwrap();
    result.unwrap();
    cleanup.unwrap();
    assert!(started.elapsed() < std::time::Duration::from_secs(5));
}

#[test]
fn grandchild_group_killed_even_after_leader_exit() {
    let mut sentinel_cmd = Command::new("sleep");
    sentinel_cmd.arg("30");
    let mut sentinel = UnixChild::spawn(&mut sentinel_cmd).unwrap();
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
    let _sentinel_cleanup = Cleanup(sentinel.pgid);
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
    assert!(sentinel.try_wait().unwrap().is_none());
    sentinel.kill().unwrap();
    sentinel.child.wait().unwrap();
}

#[test]
fn watch_esrch_latches_and_never_signals_again() {
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "exit 0"]);
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    child.child.wait().unwrap();
    assert!(killpg(child.pgid, libc::SIGTERM).is_ok());
    assert!(killpg(child.pgid, libc::SIGKILL).is_ok());
    assert!(child.empty().unwrap());
    assert!(child.gone);
    child.pgid = unsafe { libc::getpgrp() };
    assert!(child.empty().unwrap());
    child.kill().unwrap();
}
