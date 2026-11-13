use super::*;
use std::{
    io::Write,
    net::{Ipv4Addr, TcpListener, TcpStream},
    process::{Child, Command},
};

const MODE: &str = "PG_DIFF_PRODUCER_MODE";
const CONTROL: &str = "PG_DIFF_PRODUCER_CONTROL";

// A real native producer, including on Windows. Its checkout handle stays open
// until output has drained; no shell, pipe-capacity guess or Git timing required.
#[test]
fn controlled_stdout_producer() {
    let Some(mode) = std::env::var_os(MODE) else {
        return;
    };
    let mut control = TcpStream::connect(std::env::var(CONTROL).unwrap()).unwrap();
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
    std::process::exit(0);
}

fn producer(mode: &str) -> (std::path::PathBuf, Child, TcpStream) {
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
    let child = command.spawn().unwrap();
    let (mut control, _) = listener.accept().unwrap();
    control
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut ready = [0; 5];
    control.read_exact(&mut ready).unwrap();
    assert_eq!(&ready, b"ready");
    (root, child, control)
}

pub(super) fn oversized_stdout_is_drained_before_checkout_cleanup() {
    let (root, child, mut control) = producer("drain");
    let (tx, rx) = std::sync::mpsc::channel();
    let collector = std::thread::spawn(move || {
        tx.send(collect_diff(child, 1, Duration::from_secs(5)))
            .unwrap();
    });
    // The child has already emitted cap+1 but deliberately retains its handle.
    // An early TooLarge is a failure, not an invitation to retry cleanup.
    assert_eq!(
        rx.recv_timeout(Duration::from_millis(100)),
        Err(std::sync::mpsc::RecvTimeoutError::Timeout)
    );
    control.write_all(&[1]).unwrap();
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(5)).unwrap(),
        Err(InlineDiffError::TooLarge)
    );
    collector.join().unwrap();
    assert_eq!(std::fs::read(root.join("drained")).unwrap(), b"done\n");
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn oversized_hung_producer_is_a_bounded_timeout_not_too_large() {
    let (root, child, _control) = producer("hung");
    let start = std::time::Instant::now();
    assert_eq!(
        collect_diff(child, 1, Duration::from_millis(30)),
        Err(InlineDiffError::Git)
    );
    assert!(start.elapsed() < Duration::from_millis(500));
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn eof_and_reader_join_do_not_extend_child_deadline() {
    let (root, child, _control) = producer("closed");
    let start = std::time::Instant::now();
    assert_eq!(
        collect_diff(child, 4096, Duration::from_millis(30)),
        Err(InlineDiffError::Git)
    );
    assert!(start.elapsed() < Duration::from_millis(500));
    std::fs::remove_dir_all(root).unwrap();
}
