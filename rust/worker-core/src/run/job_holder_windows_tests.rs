use super::*;

#[test]
fn bounds() {
    assert_eq!(validate_max_s(None), Ok(28800));
    for s in ["600", "172800"] {
        assert!(validate_max_s(Some(s)).is_ok());
    }
    for s in ["599", "172801", "NaN", "inf", "-1", ""] {
        assert!(validate_max_s(Some(s)).is_err());
    }
}
#[test]
fn deadlines_and_tickets() {
    assert_eq!(deadline_ms(10, 600), Some(600010));
    assert_eq!(deadline_ms(u64::MAX, 1), None);
    assert_eq!(deadline_ms(1, u64::MAX), None);
    assert_eq!(parse_ticket("42 600010\n"), Some((42, 600010)));
    for s in ["", "0 1", "1 0", "1 2 3", "-1 2"] {
        assert!(parse_ticket(s).is_none());
    }
}
#[test]
fn restrictive_dacl() {
    assert_eq!(
        job_dacl("S-1-5-21-123"),
        Some("D:P(A;;GA;;;SY)(A;;GA;;;S-1-5-21-123)".into())
    );
    for s in ["", "S-1-5-", "S-1-5-21)(A;;GA;;;WD)"] {
        assert!(job_dacl(s).is_none());
    }
}
#[test]
fn expiry_decision() {
    assert!(!expired(None, 100));
    assert!(!expired(Some(101), 100));
    assert!(expired(Some(100), 100));
    assert!(expired(Some(99), 100));
}

#[cfg(windows)]
#[test]
fn native_dead_pid_then_live_handover_expires() {
    use crate::run::{group::job_name, group_windows_tests::*};
    use crate::watch::windows::{JobApi, NativeJob};
    use std::{os::windows::io::AsRawHandle, sync::atomic::Ordering, time::Duration};
    use windows_sys::Win32::System::Threading::*;
    let unit = format!("pg-win-live-handover-{}", std::process::id());
    let (mut g, _dir) = launch_bounded(&unit, "sleep", false, false, Some(1)).unwrap();
    let name = job_name(&unit);
    let slot = RequestSlot::open(&name, false).unwrap();
    let event = adoption_event(&name, false, false).unwrap();
    // PID 0 is never a process; the largest non-reserved, unaligned PID cannot exist.
    for pid in [0, u32::MAX - 2] {
        slot.pid().store(pid, Ordering::SeqCst);
        assert_ne!(unsafe { SetEvent(event.as_raw_handle()) }, 0);
        std::thread::sleep(Duration::from_millis(250));
        assert!(!gone(g.child.id()));
        until(|| slot.pid().load(Ordering::SeqCst) == 0);
        assert_eq!(slot.pid().load(Ordering::SeqCst), 0);
        assert_ne!(unsafe { ResetEvent(event.as_raw_handle()) }, 0);
        slot.pid().store(0, Ordering::SeqCst);
    }
    let mut adopter = std::process::Command::new("cmd.exe")
        .args(["/C", "more"])
        .stdin(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    slot.pid().store(adopter.id(), Ordering::SeqCst);
    assert_ne!(unsafe { SetEvent(event.as_raw_handle()) }, 0);
    let outcome = outcome_event(&name, false).unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(outcome.as_raw_handle(), 5000) },
        0
    );
    assert_ne!(unsafe { ResetEvent(event.as_raw_handle()) }, 0);
    slot.pid().store(0, Ordering::SeqCst);
    adopter.kill().unwrap();
    adopter.wait().unwrap();
    // Original owner is still alive, but must no longer suppress orphan expiry.
    until(|| gone(g.child.id()));
    g.child.wait().unwrap();
    assert!(!NativeJob::open(&name).unwrap().unwrap().active().unwrap());
}
