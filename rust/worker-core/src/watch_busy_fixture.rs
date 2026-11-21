//! Busy-loop fixture of the watch_blind tests: a TERM-ignoring shell that never outlives
//! its harness, with a drop guard and the tests that prove it.
use super::blind_tests::run_timeout;
use std::{
    process::Command,
    time::{Duration, Instant},
};

// A TERM-ignoring busy loop that ends by itself: when the harness that spawned it dies
// ($PPID gone, checked every 20000 turns) and after 60 s at the latest.
const BUSY_LOOP: &str = "P=$PPID; t=$(date +%s); n=0; while :; do n=$((n+1)); \
[ $((n % 20000)) -eq 0 ] || continue; kill -0 $P 2>/dev/null || exit 0; \
[ $(($(date +%s) - t)) -lt 60 ] || exit 0; done";
// Records the fixture shell's pid (its process group) so Drop can SIGKILL the group on
// every exit path, panic included.
pub(super) struct BusyFixture(std::path::PathBuf);
impl BusyFixture {
    pub(super) fn new() -> Self {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        Self(std::env::temp_dir().join(format!(
            "watch-busy-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        )))
    }
    pub(super) fn script(&self, trap: &str) -> String {
        format!("echo $$ > '{}'; trap {trap}; {BUSY_LOOP}", self.0.display())
    }
    pub(super) fn term_ignorer(&self) -> String {
        self.script("'' TERM")
    }
    fn pid(&self) -> Option<i32> {
        std::fs::read_to_string(&self.0).ok()?.trim().parse().ok()
    }
}
impl Drop for BusyFixture {
    fn drop(&mut self) {
        if let Some(pid) = self.pid() {
            assert!(pid > 1);
            // The supervisor made the shell a group leader: pgid == pid.
            unsafe {
                libc::killpg(pid, libc::SIGKILL);
            }
        }
        let _ = std::fs::remove_file(&self.0);
    }
}

const KILLED_HARNESS_ENV: &str = "WATCH_BLIND_KILLED_HARNESS_PIDFILE";

// Inner half of the killed-harness test: a no-op unless the outer test re-runs this
// binary with the pid file named, then it supervises a busy loop until it is SIGKILLed.
#[test]
fn killed_harness_inner_supervises_busy_loop() {
    let Some(path) = std::env::var_os(KILLED_HARNESS_ENV) else {
        return;
    };
    let fixture = BusyFixture(path.into());
    let _ = run_timeout(
        &fixture.term_ignorer(),
        false,
        b"",
        false,
        Duration::from_secs(60),
    );
    std::mem::forget(fixture); // the outer test owns the cleanup
}

fn process_alive(pid: i32) -> bool {
    if unsafe { libc::kill(pid, 0) } != 0 {
        return false;
    }
    // An unreaped zombie no longer burns CPU.
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .map(|stat| {
            !stat
                .rsplit(')')
                .next()
                .unwrap_or("")
                .trim_start()
                .starts_with('Z')
        })
        .unwrap_or(true)
}

#[test]
fn killed_harness_leaves_no_term_ignoring_child() {
    let fixture = BusyFixture::new();
    let mut harness = Command::new(std::env::current_exe().unwrap())
        .args(["--nocapture"])
        .arg("killed_harness_inner_supervises_busy_loop")
        .env(KILLED_HARNESS_ENV, &fixture.0)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let pid = loop {
        if let Some(pid) = fixture.pid() {
            break pid;
        }
        assert!(Instant::now() < deadline, "fixture never started");
        std::thread::sleep(Duration::from_millis(10));
    };
    assert!(process_alive(pid));
    harness.kill().unwrap();
    harness.wait().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while process_alive(pid) {
        assert!(
            Instant::now() < deadline,
            "busy-loop child outlived its killed harness"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn busy_fixture_drop_kills_the_group_on_unwind() {
    let fixture = BusyFixture::new();
    let mut child = Command::new("sh");
    child.args([
        "-c",
        &format!("echo $$ > '{}'; exec sleep 20", fixture.0.display()),
    ]);
    std::os::unix::process::CommandExt::process_group(&mut child, 0);
    let mut child = child.spawn().unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while fixture.pid().is_none() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let result = std::panic::catch_unwind(|| {
        let _guard = fixture;
        panic!("exercise fixture cleanup");
    });
    assert!(result.is_err());
    let deadline = Instant::now() + Duration::from_secs(1);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            panic!("guard left the busy fixture alive");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
