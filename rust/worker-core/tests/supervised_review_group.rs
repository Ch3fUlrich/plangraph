#![cfg(target_os = "linux")]

use pg_worker_core::stall::{supervise_redirected, Opts};
use std::{
    fs,
    io::ErrorKind,
    path::Path,
    process::Command,
    time::{Duration, Instant},
};

#[test]
fn review_exit_timeout_and_cancel_reap_tests_before_checkout_removal() {
    let temp = tempfile::tempdir().unwrap();
    for (mode, expected) in [
        ("exit", None),
        ("timeout", Some(ErrorKind::TimedOut)),
        ("cancel", Some(ErrorKind::Interrupted)),
    ] {
        let checkout = temp.path().join(mode);
        fs::create_dir(&checkout).unwrap();
        let pidfile = checkout.join("pid");
        let mut command = Command::new("python3");
        command
            .current_dir(&checkout)
            .arg("-c")
            .arg(
                r#"import os, pathlib, signal, sys, time
child = os.fork()
if child == 0:
    # GNU timeout can put the command in a second process group in this session.
    os.setpgid(0, 0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    pathlib.Path("pid").write_text(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
while not pathlib.Path("pid").exists():
    time.sleep(.01)
if sys.argv[1] != "exit":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
"#,
            )
            .arg(mode);
        let result = supervise_redirected(
            command,
            &Opts {
                timeout: Some(Duration::from_millis(500)),
                grace: Duration::from_millis(150),
                ..Opts::default()
            },
            || Ok(mode == "cancel" && pidfile.is_file()),
        );
        assert_eq!(
            result.as_ref().err().map(|err| err.kind()),
            expected,
            "{mode}"
        );
        let pid = fs::read_to_string(pidfile).unwrap();
        let stat = Path::new("/proc").join(pid.trim()).join("stat");
        let deadline = Instant::now() + Duration::from_secs(2);
        while fs::read_to_string(&stat)
            .is_ok_and(|s| !s.rsplit_once(") ").unwrap().1.starts_with('Z'))
        {
            if Instant::now() >= deadline {
                let _ = Command::new("kill").args(["-KILL", pid.trim()]).output();
                panic!("{mode} left the test alive");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        fs::remove_dir_all(&checkout).unwrap();
        assert!(!checkout.exists());
    }
}
