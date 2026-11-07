use super::*;
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
    assert!(job.empty().unwrap());
    job.terminate().unwrap();
    assert!(job.api.as_ref().unwrap().active);
    assert!(job.cpu().is_err());
}
#[test]
fn accounting_and_termination_use_retained_job() {
    let mut job = AdoptedJob::verify(Some(fake(true)), 42, 99).unwrap();
    assert_eq!(job.cpu().unwrap().total_ms, 5);
    job.terminate().unwrap();
    assert!(job.empty().unwrap());
    job.api.as_mut().unwrap().active = true;
    assert!(job.empty().unwrap());
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
