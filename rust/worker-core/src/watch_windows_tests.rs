use super::*;
#[cfg(windows)]
#[test]
fn timeout_then_adopt() {
    use crate::run::job_holder_windows::*;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{Foundation::WAIT_TIMEOUT, System::Threading::*};
    let name = format!("Local\\pg-timeout-{}", std::process::id());
    let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
    let _job = secure_job(wide.as_ptr()).unwrap();
    let _event = adoption_event(&name, false, true).unwrap();
    let _closing = adoption_event(&name, true, true).unwrap();
    let _outcome = outcome_event(&name, true).unwrap();
    let slot = RequestSlot::open(&name, true).unwrap();
    slot.holder_pid()
        .store(std::process::id(), std::sync::atomic::Ordering::SeqCst);
    let mut api = NativeJob::open(&name).unwrap().unwrap();
    assert!(api.adopted().is_err());
    assert_eq!(slot.pid().load(std::sync::atomic::Ordering::SeqCst), 0);
    assert_eq!(
        unsafe { WaitForSingleObject(_event.as_raw_handle(), 0) },
        WAIT_TIMEOUT
    );
    let helper = std::thread::spawn(move || {
        let event = adoption_event(&name, false, false).unwrap();
        let outcome = outcome_event(&name, false).unwrap();
        let slot = RequestSlot::open(&name, false).unwrap();
        assert_eq!(
            unsafe { WaitForSingleObject(event.as_raw_handle(), 2000) },
            0
        );
        let pid = slot.pid().load(std::sync::atomic::Ordering::SeqCst);
        assert!(claim_adoption(slot.pid(), pid));
        assert_ne!(unsafe { SetEvent(outcome.as_raw_handle()) }, 0);
    });
    api.adopted().unwrap();
    helper.join().unwrap();
    assert_eq!(slot.pid().load(std::sync::atomic::Ordering::SeqCst), 0);
    assert_eq!(
        unsafe { WaitForSingleObject(_event.as_raw_handle(), 0) },
        WAIT_TIMEOUT
    );
}
#[cfg(windows)]
#[test]
fn committed_holder_exit_returns_error() {
    use crate::run::job_holder_windows::*;
    use std::{os::windows::io::AsRawHandle, sync::atomic::Ordering};
    use windows_sys::Win32::{Foundation::WAIT_OBJECT_0, System::Threading::*};
    let name = format!("Local\\pg-holder-exit-{}", std::process::id());
    let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
    let _job = secure_job(wide.as_ptr()).unwrap();
    let event = adoption_event(&name, false, true).unwrap();
    let _closing = adoption_event(&name, true, true).unwrap();
    let _outcome = outcome_event(&name, true).unwrap();
    let slot = RequestSlot::open(&name, true).unwrap();
    let mut holder = std::process::Command::new("cmd.exe")
        .args(["/C", "more"])
        .stdin(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    slot.holder_pid().store(holder.id(), Ordering::SeqCst);
    let worker = std::thread::spawn(move || NativeJob::open(&name).unwrap().unwrap().adopted());
    assert_eq!(
        unsafe { WaitForSingleObject(event.as_raw_handle(), 2000) },
        WAIT_OBJECT_0
    );
    let pid = slot.pid().load(Ordering::SeqCst);
    assert!(claim_adoption(slot.pid(), pid));
    holder.kill().unwrap();
    holder.wait().unwrap();
    fn until(mut ready: impl FnMut() -> bool) {
        let end = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !ready() {
            assert!(
                std::time::Instant::now() < end,
                "adoption hung after holder exit"
            );
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }
    until(|| worker.is_finished());
    assert!(worker.join().unwrap().is_err());
}
#[cfg(windows)]
#[test]
fn watch_recorder_start_matches_creation_filetime() {
    use crate::run::{record::RunRecord, supervise::*};
    use std::{os::windows::io::FromRawHandle, time::Duration};
    use windows_sys::Win32::System::Threading::*;

    let dir = tempfile::tempdir().unwrap();
    let rt = Runtime::new(Config {
        state_dir: dir.path().into(),
        grace: Duration::ZERO,
        ..Default::default()
    });
    struct Guard(Option<RunHandle>);
    impl Drop for Guard {
        fn drop(&mut self) {
            if let Some(handle) = self.0.take() {
                handle.cancel();
                handle.join().unwrap();
            }
        }
    }
    let _guard = Guard(Some(
        start(
            &rt,
            RunSpec {
                run_id: "run-watchfiletime".into(),
                job_id: "job-watchfiletime".into(),
                program: "cmd.exe".into(),
                args: vec!["/C".into(), "ping -n 60 127.0.0.1 >NUL".into()],
                cwd: dir.path().into(),
                env: ["SystemRoot", "PATH"]
                    .into_iter()
                    .filter_map(|key| std::env::var_os(key).map(|value| (key.into(), value)))
                    .collect(),
                stdin: None,
                base: Default::default(),
                events: vec![],
                finish: None,
            },
        )
        .unwrap(),
    ));
    let path = dir.path().join("runs/run-watchfiletime/record.json");
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    let record = loop {
        let record = RunRecord::read_path(&path).unwrap().to_value();
        if record["status"] == "running" {
            break record;
        }
        assert_eq!(record["status"], "queued", "launch failed: {record}");
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    };
    let pid = u32::try_from(record["proc"]["pid"].as_u64().unwrap()).unwrap();
    // SAFETY: query-only handle for the live recorded child, owned below.
    let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    assert!(!raw.is_null());
    let process = unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(raw) };
    use std::os::windows::io::AsRawHandle;
    let (mut created, mut exit, mut kernel, mut user) = Default::default();
    // SAFETY: live owned process handle and writable FILETIME outputs.
    assert_ne!(
        unsafe {
            GetProcessTimes(
                process.as_raw_handle(),
                &mut created,
                &mut exit,
                &mut kernel,
                &mut user,
            )
        },
        0
    );
    let creation = (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime);
    assert_eq!(record["proc"]["start_time"].as_u64(), Some(creation));
    assert_eq!(record["pid_start"].as_u64(), Some(creation));
    let mut job = NativeJob::open(record["proc"]["group"].as_str().unwrap())
        .unwrap()
        .unwrap();
    assert!(job.member(pid, creation).unwrap());
}
#[cfg(windows)]
#[test]
fn native_membership_and_accounting() {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::System::{JobObjects::*, Threading::*};
    let name = format!("Local\\pg-test-adopt-{}", std::process::id());
    let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
    // SAFETY: valid terminated unique name; test retains the Job through queries.
    let raw = unsafe { CreateJobObjectW(std::ptr::null(), wide.as_ptr()) };
    assert!(!raw.is_null());
    let job = unsafe { OwnedHandle::from_raw_handle(raw) };
    let mut child = std::process::Command::new("cmd.exe")
        .args(["/C", "more"])
        .stdin(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    struct Guard(std::process::Child);
    impl Drop for Guard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let stdin = child.stdin.take();
    let child = Guard(child);
    assert_ne!(
        unsafe { AssignProcessToJobObject(job.as_raw_handle(), child.0.as_raw_handle()) },
        0
    );
    let (mut created, mut exit, mut kernel, mut user) = Default::default();
    assert_ne!(
        unsafe {
            GetProcessTimes(
                child.0.as_raw_handle(),
                &mut created,
                &mut exit,
                &mut kernel,
                &mut user,
            )
        },
        0
    );
    let start = (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime);
    let mut api = NativeJob::open(&name).unwrap().unwrap();
    assert!(api.member(child.0.id(), start).unwrap());
    assert!(!api.member(child.0.id(), start + 1).unwrap());
    let mut adopted = AdoptedJob::verify(Some(api), child.0.id(), start).unwrap();
    assert!(adopted.cpu().is_ok());
    adopted.terminate().unwrap();
    drop(stdin);
}
struct Fake {
    member: bool,
    active: bool,
    times: (i64, i64),
    error: bool,
}
impl JobApi for Fake {
    fn member(&mut self, pid: u32, start: u64) -> io::Result<bool> {
        assert_eq!((pid, start), (42, 99));
        if self.error {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        Ok(self.member)
    }
    fn active(&mut self) -> io::Result<bool> {
        Ok(self.active)
    }
    fn times(&mut self) -> io::Result<(i64, i64)> {
        Ok(self.times)
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.active = false;
        Ok(())
    }
}
fn fake(member: bool) -> Fake {
    Fake {
        member,
        active: true,
        times: (30_000, 20_000),
        error: false,
    }
}
#[test]
fn missing_and_nonmember_jobs_are_gone() {
    assert!(AdoptedJob::<Fake>::verify(None, 42, 99)
        .unwrap()
        .empty()
        .unwrap());
    let mut job = AdoptedJob::verify(Some(fake(false)), 42, 99).unwrap();
    assert!(job.is_gone().unwrap());
    assert!(!job.empty().unwrap());
    job.terminate().unwrap();
    assert!(job.empty().unwrap());
    assert!(job.cpu().is_err());
}
#[test]
fn accounting_and_termination_use_retained_job() {
    let mut job = AdoptedJob::verify(Some(fake(true)), 42, 99).unwrap();
    assert_eq!(job.cpu().unwrap().total_ms, 5);
    job.terminate().unwrap();
    assert!(job.empty().unwrap());
    job.api.as_mut().unwrap().active = true;
    assert!(!job.empty().unwrap());
}
#[test]
fn errors_and_invalid_accounting_are_not_activity() {
    let mut api = fake(true);
    api.error = true;
    assert!(AdoptedJob::verify(Some(api), 42, 99).is_err());
    let mut job = AdoptedJob::verify(Some(fake(true)), 42, 99).unwrap();
    job.api.as_mut().unwrap().times = (-1, 10);
    assert!(job.cpu().is_err());
    job.api.as_mut().unwrap().times = (i64::MAX, i64::MAX);
    assert!(job.cpu().is_ok());
}
#[test]
fn rejection_and_admission_have_one_winner() {
    use std::sync::atomic::{AtomicU32, Ordering};
    let slot = AtomicU32::new(42);
    assert_eq!(slot.swap(u32::MAX - 1, Ordering::SeqCst), 42);
    assert!(!super::claim_adoption(&slot, 42));
    slot.store(42, Ordering::SeqCst);
    assert!(super::claim_adoption(&slot, 42));
    assert_eq!(slot.swap(u32::MAX - 1, Ordering::SeqCst), u32::MAX);
}
