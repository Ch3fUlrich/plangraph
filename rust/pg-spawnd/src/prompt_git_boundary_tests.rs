use super::*;
use std::cell::Cell;

#[test]
fn completed_collection_is_not_retimed_at_the_deadline_boundary() {
    let deadline = Instant::now();
    let samples = Cell::new(0);
    let ready = Cell::new(false);
    let now = || {
        samples.set(samples.get() + 1);
        if samples.get() > 2 {
            deadline
        } else {
            deadline - Duration::from_nanos(1)
        }
    };
    let completed =
        wait_before_deadline(deadline, now, |_| ready.set(true), || Ok(ready.get())).unwrap();
    assert!(completed);
    assert!(ready.get(), "must have waited for completion");
    assert_eq!(samples.get(), 2, "do not resample after observing success");
}

#[test]
fn completion_at_or_after_the_deadline_gets_no_grace() {
    let deadline = Instant::now();
    for delay in [Duration::ZERO, Duration::from_nanos(1)] {
        let completed = wait_before_deadline(
            deadline,
            || deadline + delay,
            |_| panic!("deadline cannot add a sleep"),
            || Ok(true),
        )
        .unwrap();
        assert!(!completed);
    }
}

#[cfg(unix)]
#[test]
fn collection_errors_terminate_and_reap_the_owned_child() {
    for (mode, fail_at, expected_polls) in [
        ("closed", 2, 3),
        ("closed", 1, 2),
        ("hung", 2, 2),
        ("hung", 0, 1),
    ] {
        let (root, mut child, mut control) = race::producer(mode);
        let pid = child.id() as i32;
        if fail_at == 0 {
            drop(child.stdout.take());
        }
        let (tx, rx) = mpsc::channel();
        let collector = std::thread::spawn(move || {
            let mut polls = 0;
            let result = collect_diff_with_poll(
                child,
                4096,
                if mode == "closed" {
                    Duration::from_secs(30)
                } else {
                    Duration::from_millis(30)
                },
                Instant::now,
                std::thread::sleep,
                |child| {
                    polls += 1;
                    if fail_at != 0 && polls >= fail_at {
                        Err(std::io::ErrorKind::Other.into())
                    } else {
                        child.try_wait()
                    }
                },
            );
            tx.send((result, polls)).unwrap();
        });
        let outcome = rx.recv_timeout(Duration::from_secs(3));
        let reaped = unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) };
        let wait_error = std::io::Error::last_os_error().raw_os_error();
        // Clean up even a failing-first leak or a cleanup-poll error that blocks
        // in wait; record the pre-cleanup result so cleanup cannot pass the test.
        if reaped == 0 {
            unsafe { libc::kill(-pid, libc::SIGKILL) };
            collector.join().unwrap();
            unsafe { libc::waitpid(pid, std::ptr::null_mut(), 0) };
        } else {
            collector.join().unwrap();
        }
        let mut byte = [0];
        assert!(matches!(control.read(&mut byte), Ok(0)));
        assert!(!root.join("drained").exists());
        std::fs::remove_dir_all(root).unwrap();
        let (result, polls) = outcome.expect("collection error must not hang in cleanup");
        assert_eq!(result, Err(InlineDiffError::Git));
        assert_eq!(reaped, -1, "mode={mode} fail_at={fail_at}: leaked child");
        assert_eq!(wait_error, Some(libc::ECHILD));
        assert_eq!(
            polls, expected_polls,
            "must reach collection and cleanup polls"
        );
    }
}
