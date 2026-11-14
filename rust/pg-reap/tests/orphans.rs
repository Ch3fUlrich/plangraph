#![cfg(target_os = "linux")]
use pg_reap::orphans::{inspect, Evidence};
use std::{fs, path::Path};
#[test]
fn requires_orphan_start_and_exact_cwd() {
    let root = tempfile::tempdir().unwrap();
    let cwd = tempfile::tempdir().unwrap();
    let proc = root.path().join("123");
    fs::create_dir(&proc).unwrap();
    let mut fields = vec!["0"; 22];
    fields[0] = "S";
    fields[1] = "1";
    fields[19] = "42";
    fs::write(
        proc.join("stat"),
        format!("123 (test) {}", fields.join(" ")),
    )
    .unwrap();
    std::os::unix::fs::symlink(cwd.path(), proc.join("cwd")).unwrap();
    let mut evidence = Evidence {
        pid: 123,
        start_ticks: 42,
        cwd: cwd.path().into(),
    };
    assert!(inspect(root.path(), &evidence).unwrap());
    evidence.start_ticks = 43;
    assert!(!inspect(root.path(), &evidence).unwrap());
    evidence.start_ticks = 42;
    evidence.cwd = root.path().into();
    assert!(!inspect(root.path(), &evidence).unwrap());
    fields[1] = "2";
    fs::write(
        proc.join("stat"),
        format!("123 (test) {}", fields.join(" ")),
    )
    .unwrap();
    evidence.cwd = cwd.path().into();
    assert!(!inspect(root.path(), &evidence).unwrap());
}

#[test]
fn reap_kills_only_the_recorded_real_orphan() {
    use pg_reap::orphans::reap;
    use std::{
        io::{BufRead, BufReader, Write},
        os::fd::{AsRawFd, FromRawFd, OwnedFd},
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    struct Cleanup(OwnedFd);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            unsafe {
                libc::syscall(
                    libc::SYS_pidfd_send_signal,
                    self.0.as_raw_fd(),
                    libc::SIGKILL,
                    std::ptr::null::<libc::siginfo_t>(),
                    0,
                );
            }
        }
    }
    let cwd = tempfile::tempdir().unwrap();
    let mut parent = Command::new("sh")
        .args(["-c", "sleep 30 & echo $!; read release"])
        .current_dir(cwd.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(parent.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let pid: u32 = line.trim().parse().unwrap();
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
    assert!(fd >= 0, "test requires the same pidfd support as reap");
    let _cleanup = Cleanup(unsafe { OwnedFd::from_raw_fd(fd) });
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let start_ticks = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let mut evidence = Evidence {
        pid,
        start_ticks,
        cwd: cwd.path().into(),
    };
    let live_parent_result = reap(&evidence);
    parent
        .stdin
        .take()
        .unwrap()
        .write_all(b"release\n")
        .unwrap();
    assert!(parent.wait().unwrap().success());
    assert!(
        !live_parent_result.unwrap(),
        "a living parent prevents orphan reaping"
    );
    let deadline = Instant::now() + Duration::from_secs(2);
    while !inspect(Path::new("/proc"), &evidence).unwrap() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(2));
    }
    assert!(inspect(Path::new("/proc"), &evidence).unwrap());
    evidence.start_ticks += 1;
    assert!(
        !reap(&evidence).unwrap(),
        "wrong identity must never be signalled"
    );
    evidence.start_ticks -= 1;
    assert!(reap(&evidence).unwrap());
    assert!(!inspect(Path::new("/proc"), &evidence).unwrap());
}
