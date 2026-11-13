use super::*;
use std::{
    io::Write,
    net::{Ipv4Addr, TcpListener, TcpStream},
    process::{Child, Command},
};

const MODE: &str = "PG_DIFF_PRODUCER_MODE";
const CONTROL: &str = "PG_DIFF_PRODUCER_CONTROL";
// Setup/watchdog bounds allow scheduling delays; they are not assertions about
// how quickly the 30 ms production deadline must run on a loaded runner.
const WATCHDOG: Duration = Duration::from_secs(30);
const PRODUCER_WATCHDOG: Duration = Duration::from_secs(120);

// A real native producer, including on Windows. Its checkout handle stays open
// until output has drained; no shell, pipe-capacity guess or Git timing required.
#[test]
fn controlled_stdout_producer() {
    let Some(mode) = std::env::var_os(MODE) else {
        return;
    };
    #[cfg(unix)]
    if mode == "escaped" || mode == "exited" || mode == "group" {
        use std::os::unix::process::CommandExt;
        let mut holder = Command::new(std::env::current_exe().unwrap());
        holder.args([
            "--exact",
            "prompt_git::tests::race::controlled_stdout_producer",
            "--nocapture",
        ]);
        holder.env(MODE, "holder");
        if mode == "escaped" {
            holder.process_group(0);
        } else if mode == "exited" {
            // A same-group survivor must not keep the exited child's pipe open.
            holder.stdout(Stdio::null());
            std::io::stdout().write_all(b"oversized").unwrap();
        }
        let mut holder = holder.spawn().unwrap();
        if mode == "group" {
            // The owned wrapper and its stdout-holding descendant must both
            // be terminated by the collector's group cleanup.
            holder.wait().unwrap();
        }
        std::process::exit(0);
    }
    assert_ne!(mode, "panic", "injected panic before control connection");
    let address = std::env::var(CONTROL).unwrap().parse().unwrap();
    let mut control = TcpStream::connect_timeout(&address, WATCHDOG).unwrap();
    control.set_read_timeout(Some(PRODUCER_WATCHDOG)).unwrap();
    control.set_write_timeout(Some(WATCHDOG)).unwrap();
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0);
    }
    let held = options.open("held").unwrap();
    std::io::stdout().write_all(&[b'x'; 1024]).unwrap();
    std::io::stdout().flush().unwrap();
    if mode == "closed" {
        #[cfg(unix)]
        unsafe {
            // SAFETY: this disposable child will exit without using stdout again.
            libc::close(libc::STDOUT_FILENO);
        }
        #[cfg(windows)]
        unsafe {
            use std::os::windows::io::AsRawHandle;
            // SAFETY: only this disposable child's inherited stdout is closed.
            windows_sys::Win32::Foundation::CloseHandle(std::io::stdout().as_raw_handle());
        }
    }
    control.write_all(b"ready").unwrap();
    let mut go = [0];
    control.read_exact(&mut go).unwrap();
    assert_eq!(go, [1]);
    std::io::stdout()
        .write_all(&vec![b'x'; 1024 * 1024])
        .unwrap();
    std::io::stdout().flush().unwrap();
    drop(held);
    std::fs::write("drained", b"done\n").unwrap();
    control.write_all(b"done").unwrap();
    std::process::exit(0);
}

fn accept_control(listener: &TcpListener, timeout: Duration) -> std::io::Result<TcpStream> {
    listener.set_nonblocking(true)?;
    let deadline = Instant::now() + timeout;
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                // Windows accepted sockets can inherit listener nonblocking
                // mode; restore blocking IO before applying socket timeouts.
                stream.set_nonblocking(false)?;
                return Ok(stream);
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => (),
            Err(e) => return Err(e),
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(std::io::ErrorKind::TimedOut.into());
        }
        std::thread::sleep(remaining.min(Duration::from_millis(5)));
    }
}

#[test]
fn producer_connection_timeout_does_not_require_a_child_connection() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    assert_eq!(
        accept_control(&listener, Duration::ZERO)
            .unwrap_err()
            .kind(),
        std::io::ErrorKind::TimedOut
    );
}

#[test]
fn failed_kill_still_waits_for_and_reaps_the_child() {
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "prompt_git::tests::race::controlled_stdout_producer",
        ])
        .env_remove(MODE)
        .env_remove(CONTROL)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    #[cfg(unix)]
    let pid = child.id() as i32;
    let result = reap_after_kill(&mut child, |_| {
        Err(std::io::ErrorKind::PermissionDenied.into())
    });
    #[cfg(unix)]
    let reaped = unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) };
    // Always clean up, including with the failing-first propagation variant.
    let status = child.wait().unwrap();
    assert_eq!(result.unwrap(), status);
    assert!(status.success());
    #[cfg(unix)]
    assert_eq!(reaped, -1, "helper must reap before returning");
}

#[test]
fn producer_panic_before_connect_fails_setup_instead_of_hanging() {
    assert!(
        std::panic::catch_unwind(|| producer_with_timeout("panic", Duration::from_millis(30)))
            .is_err()
    );
}

fn producer(mode: &str) -> (std::path::PathBuf, Child, TcpStream) {
    producer_with_timeout(mode, WATCHDOG)
}

fn producer_with_timeout(mode: &str, timeout: Duration) -> (std::path::PathBuf, Child, TcpStream) {
    let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("held"), b"checkout handle").unwrap();
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args([
            "--exact",
            "prompt_git::tests::race::controlled_stdout_producer",
            "--nocapture",
        ])
        .env(MODE, mode)
        .env(CONTROL, listener.local_addr().unwrap().to_string())
        .current_dir(&root)
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().unwrap();
    let setup = || -> std::io::Result<TcpStream> {
        let mut control = accept_control(&listener, timeout)?;
        control.set_read_timeout(Some(WATCHDOG))?;
        control.set_write_timeout(Some(WATCHDOG))?;
        let mut ready = [0; 5];
        control.read_exact(&mut ready)?;
        assert_eq!(&ready, b"ready");
        Ok(control)
    };
    let control = setup().unwrap_or_else(|e| {
        let _ = child.kill();
        child.wait().unwrap();
        std::fs::remove_dir_all(&root).unwrap();
        panic!("producer setup failed: {e}");
    });
    (root, child, control)
}

pub(super) fn oversized_stdout_is_drained_before_checkout_cleanup() {
    let (root, child, mut control) = producer("drain");
    let (tx, rx) = std::sync::mpsc::channel();
    let collector = std::thread::spawn(move || {
        tx.send(collect_diff(child, 1, WATCHDOG)).unwrap();
    });
    // The child has already emitted cap+1 but deliberately retains its handle.
    // An early TooLarge is a failure, not an invitation to retry cleanup.
    assert_eq!(
        rx.recv_timeout(Duration::from_millis(100)),
        Err(std::sync::mpsc::RecvTimeoutError::Timeout)
    );
    control.write_all(&[1]).unwrap();
    assert_eq!(
        rx.recv_timeout(WATCHDOG).unwrap(),
        Err(InlineDiffError::TooLarge)
    );
    collector.join().unwrap();
    assert_eq!(std::fs::read(root.join("drained")).unwrap(), b"done\n");
    std::fs::remove_dir_all(root).unwrap();
}

#[cfg(unix)]
pub(super) fn oversized_exited_child_does_not_kill_group() {
    let (root, mut child, mut control) = producer("exited");
    assert!(child.wait().unwrap().success());
    assert_eq!(
        collect_diff(child, 1, WATCHDOG),
        Err(InlineDiffError::TooLarge)
    );
    // No sleep/touch race: the same-group survivor must still be connected and
    // only drops its checkout handle after we explicitly release it.
    control.write_all(&[1]).unwrap();
    let mut done = [0; 4];
    control.read_exact(&mut done).unwrap();
    assert_eq!(&done, b"done");
    assert_eq!(std::fs::read(root.join("drained")).unwrap(), b"done\n");
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn oversized_hung_producer_is_a_bounded_timeout_not_too_large() {
    deadline_ends_wait("hung", 1);
}

#[test]
fn eof_and_reader_join_do_not_extend_child_deadline() {
    deadline_ends_wait("closed", 4096);
    deadline_ends_wait_with_clock("closed", 4096, true);
}

pub(super) fn deadline_ends_wait(mode: &str, cap: usize) {
    deadline_ends_wait_with_clock(mode, cap, false);
}

fn deadline_ends_wait_with_clock(mode: &str, cap: usize, logical_clock: bool) {
    let (root, child, mut control) = producer(mode);
    #[cfg(unix)]
    let pid = child.id() as i32;
    let (tx, rx) = mpsc::channel();
    let collector = std::thread::spawn(move || {
        let (result, clock) = if logical_clock {
            use std::cell::Cell;
            // Keep the virtual deadline at this real instant, so a regression
            // using a fresh real clock for its grace is caught as well.
            let start = Instant::now() - WATCHDOG;
            let reads = Cell::new(0);
            let current = Cell::new(start);
            let slept = Cell::new(false);
            let now = || {
                reads.set(reads.get() + 1);
                // The first two samples establish the deadline and receive
                // budget. After EOF, expire it without a wall-clock race.
                if reads.get() == 3 {
                    current.set(start + WATCHDOG);
                }
                current.get()
            };
            let sleep = |_| {
                slept.set(true);
                // Let an injected one-second grace finish and reap the child
                // before asserting, rather than leaving a failing peer alive.
                current.set(current.get() + Duration::from_secs(2));
            };
            let result = collect_diff_with_clock(child, cap, WATCHDOG, now, sleep);
            (result, Some((reads.get(), slept.get())))
        } else {
            (collect_diff(child, cap, Duration::from_millis(30)), None)
        };
        tx.send((result, clock)).unwrap();
    });
    // No release is sent until after the result. The producer cannot finish
    // normally (its read watchdog is 4x our failure watchdog), so only the
    // collector's deadline can end this wait, regardless of scheduler latency.
    let (result, clock) = rx.recv_timeout(WATCHDOG).unwrap();
    assert_eq!(result, Err(InlineDiffError::Git));
    collector.join().unwrap();
    #[cfg(unix)]
    assert_eq!(
        unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) },
        -1,
        "collector must reap the owned child"
    );
    if mode == "escaped" {
        // The escaped holder is still alive: its EOF cannot have ended the
        // collection. Only now release it and wait for its cleanup handshake.
        control.write_all(&[1]).unwrap();
        let mut done = [0; 4];
        control.read_exact(&mut done).unwrap();
        assert_eq!(&done, b"done");
        assert_eq!(std::fs::read(root.join("drained")).unwrap(), b"done\n");
        std::fs::remove_dir_all(root).unwrap();
        return;
    }
    let mut byte = [0];
    match control.read(&mut byte) {
        Ok(0) => (),
        Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset => (),
        result => panic!("producer was not terminated: {result:?}"),
    }
    assert!(!root.join("drained").exists());
    std::fs::remove_dir_all(root).unwrap();
    if let Some((reads, slept)) = clock {
        assert!(reads >= 3, "EOF must reach the child-exit deadline check");
        assert!(!slept, "EOF added a wait after the original child deadline");
    }
}
