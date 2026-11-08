//! Private Windows Job keeper; the fixed deadline bounds only the orphan window.
pub fn validate_max_s(value: Option<&str>) -> Result<u64, String> {
    let seconds = value.unwrap_or("28800").parse::<u64>().ok();
    seconds
        .filter(|s| (600..=172800).contains(s))
        .ok_or_else(|| "invalid PG_SPAWND_WINDOWS_HOLDER_MAX_S (600..=172800)".into())
}
pub fn deadline_ms(now: u64, seconds: u64) -> Option<u64> {
    now.checked_add(seconds.checked_mul(1000)?)
}
pub fn parse_ticket(value: &str) -> Option<(usize, u64)> {
    let mut fields = value.split_whitespace();
    let handle = fields.next()?.parse().ok()?;
    let deadline = fields.next()?.parse().ok()?;
    (handle != 0 && deadline != 0 && fields.next().is_none()).then_some((handle, deadline))
}
pub fn job_dacl(sid: &str) -> Option<String> {
    let parts: Vec<_> = sid.split('-').collect();
    (parts.len() >= 4
        && parts[0] == "S"
        && parts[1] == "1"
        && parts[2..]
            .iter()
            .all(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())))
    .then(|| format!("D:P(A;;GA;;;SY)(A;;GA;;;{sid})"))
}
#[cfg(windows)]
pub use platform::*;
#[cfg(windows)]
mod platform {
    use super::*;
    use std::{
        io::{self, Read, Write},
        os::windows::{
            io::{AsRawHandle, FromRawHandle, OwnedHandle},
            process::CommandExt,
        },
        process::{Command, Stdio},
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };
    use windows_sys::Win32::{
        Foundation::*,
        Security::*,
        System::{
            JobObjects::*,
            SystemServices::{JOB_OBJECT_QUERY, JOB_OBJECT_TERMINATE},
            Threading::*,
        },
    };
    static HOLDER: std::sync::OnceLock<std::path::PathBuf> = std::sync::OnceLock::new();
    pub fn set_holder_path(path: std::path::PathBuf) {
        let _ = HOLDER.set(path);
    }
    pub fn holder_path() -> io::Result<std::path::PathBuf> {
        #[cfg(test)]
        return std::env::current_exe();
        #[cfg(not(test))]
        HOLDER.get().cloned().ok_or_else(|| {
            io::Error::other("JobUnavailable: register a holder binary dispatching __job-holder")
        })
    }
    #[link(name = "advapi32")]
    extern "system" {
        fn ConvertSidToStringSidW(sid: PSID, text: *mut *mut u16) -> i32;
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text: *const u16,
            revision: u32,
            descriptor: *mut PSECURITY_DESCRIPTOR,
            size: *mut u32,
        ) -> i32;
    }
    pub fn now_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }
    #[rustfmt::skip]
    pub fn secure_job(name: *const u16) -> io::Result<OwnedHandle> { secure_object(name, false) }
    #[rustfmt::skip]
    fn secure_object(name: *const u16, event: bool) -> io::Result<OwnedHandle> {
        // SAFETY: token/descriptor buffers are aligned and live through each call;
        // LocalAlloc results are freed and kernel handles transferred to RAII.
        unsafe {
            let mut token = std::ptr::null_mut();
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return Err(io::Error::last_os_error());
            }
            let token = OwnedHandle::from_raw_handle(token);
            let mut size = 0;
            GetTokenInformation(token.as_raw_handle(), TokenUser, std::ptr::null_mut(), 0, &mut size);
            let mut data = vec![0usize; (size as usize).div_ceil(std::mem::size_of::<usize>())];
            if GetTokenInformation(token.as_raw_handle(), TokenUser, data.as_mut_ptr().cast(), size, &mut size) == 0 {
                return Err(io::Error::last_os_error());
            }
            let mut text = std::ptr::null_mut();
            if ConvertSidToStringSidW((*(data.as_ptr().cast::<TOKEN_USER>())).User.Sid, &mut text) == 0 {
                return Err(io::Error::last_os_error());
            }
            let mut len = 0;
            while *text.add(len) != 0 {
                len += 1;
            }
            let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, len));
            LocalFree(text.cast());
            let sddl: Vec<u16> = job_dacl(&sid).ok_or_else(|| io::Error::other("invalid user SID"))?
                .encode_utf16().chain(Some(0)).collect();
            let mut descriptor = std::ptr::null_mut();
            if ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.as_ptr(), 1, &mut descriptor, std::ptr::null_mut()) == 0 {
                return Err(io::Error::last_os_error());
            }
            let attributes = SECURITY_ATTRIBUTES { nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: descriptor, bInheritHandle: 0 };
            super::super::group_windows::SetLastError(0);
            let raw = if event { CreateEventW(&attributes, 1, 0, name) } else { CreateJobObjectW(&attributes, name) };
            let error = GetLastError();
            LocalFree(descriptor);
            if raw.is_null() {
                return Err(io::Error::from_raw_os_error(error as i32));
            }
            let job = OwnedHandle::from_raw_handle(raw);
            if error == ERROR_ALREADY_EXISTS {
                return Err(io::Error::from_raw_os_error(error as i32));
            }
            Ok(job)
        }
    }
    pub fn adoption_event(name: &str, closing: bool, create: bool) -> io::Result<OwnedHandle> {
        // The name is scoped to the opaque run identity. Even a guessed/precreated
        // name cannot grant adoption: the holder rejects existing objects outright.
        let name: Vec<u16> = format!(
            "{name}-holder-{}",
            if closing { "closing" } else { "adopted" }
        )
        .encode_utf16()
        .chain(Some(0))
        .collect();
        if create {
            return secure_object(name.as_ptr(), true);
        }
        // Never create while observing: a missing or inaccessible event fails closed.
        let raw = unsafe { OpenEventW(0x00100002, 0, name.as_ptr()) };
        if raw.is_null() {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { OwnedHandle::from_raw_handle(raw) })
    }
    pub fn outcome_event(name: &str, create: bool) -> io::Result<OwnedHandle> {
        adoption_event(&format!("{name}-outcome"), false, create)
    }
    pub fn rejected_event(name: &str, create: bool) -> io::Result<OwnedHandle> {
        adoption_event(&format!("{name}-rejected"), false, create)
    }
    // A same-user, non-inheritable mapping carries the requesting daemon PID.
    #[link(name = "kernel32")]
    #[rustfmt::skip]
    extern "system" {
        fn CreateFileMappingW(file: HANDLE, attributes: *const SECURITY_ATTRIBUTES,
            protect: u32, high: u32, low: u32, name: *const u16) -> HANDLE;
        fn OpenFileMappingW(access: u32, inherit: i32, name: *const u16) -> HANDLE;
        fn MapViewOfFile(mapping: HANDLE, access: u32, high: u32, low: u32, size: usize) -> *mut u32;
        fn UnmapViewOfFile(address: *const u32) -> i32;
    }
    pub struct RequestSlot(OwnedHandle, *mut u32);
    #[rustfmt::skip]
    impl RequestSlot {
        pub fn open(name: &str, create: bool) -> io::Result<Self> {
            let name: Vec<u16> = format!("{name}-holder-request").encode_utf16().chain(Some(0)).collect();
            unsafe {
                let raw = if create {
                    CreateFileMappingW(INVALID_HANDLE_VALUE, std::ptr::null(), 4, 0, 4, name.as_ptr())
                } else {
                    OpenFileMappingW(6, 0, name.as_ptr())
                };
                if raw.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let handle = OwnedHandle::from_raw_handle(raw);
                if create && GetLastError() == ERROR_ALREADY_EXISTS {
                    return Err(io::Error::other("existing holder request"));
                }
                let view = MapViewOfFile(raw, 6, 0, 0, 4);
                if view.is_null() {
                    return Err(io::Error::last_os_error());
                }
                Ok(Self(handle, view))
            }
        }
        pub fn pid(&self) -> &std::sync::atomic::AtomicU32 {
            // SAFETY: aligned, shared four-byte mapping remains live through self.
            unsafe { &*self.1.cast() }
        }
    }
    #[rustfmt::skip]
    impl Drop for RequestSlot {
        fn drop(&mut self) { let _ = &self.0; unsafe { UnmapViewOfFile(self.1); } }
    }
    #[rustfmt::skip]
    /// Ok(true): breakaway was refused and the holder runs inside the caller's outer job.
    pub fn launch(holder: &std::path::Path, job: &OwnedHandle, seconds: u64, name: &str) -> io::Result<bool> {
        #[cfg(test)]
        if FAIL_LAUNCH.get() {
            return Err(io::Error::other("injected holder launch failure"));
        }
        let mut command = Command::new(holder);
        #[cfg(not(test))]
        command.arg("__job-holder");
        #[cfg(test)]
        command.args([
            "--exact",
            "run::group_windows_tests::holder_helper",
            "--nocapture",
        ]);
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        // Breakaway is refused inside a job without BREAKAWAY_OK (CI runners, some services):
        // then the holder stays in that outer job, still outliving a daemon kill.
        let mut no_breakaway = false;
        let mut child = match command.creation_flags(CREATE_SUSPENDED | CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW).spawn() {
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied => {
                no_breakaway = true;
                command.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW).spawn()
            }
            spawned => spawned,
        }
        .map_err(|e| io::Error::other(format!("JobUnavailable: holder launch refused: {e}")))?;
        let setup = (|| {
            let mut duplicate = std::ptr::null_mut();
            // Only the trusted suspended helper receives QUERY/TERMINATE, never inheritance.
            if unsafe { DuplicateHandle(GetCurrentProcess(), job.as_raw_handle(), child.as_raw_handle(),
                &mut duplicate, JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, 0, 0) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let mut adopted = std::ptr::null_mut();
            if unsafe { DuplicateHandle(GetCurrentProcess(), GetCurrentProcess(), child.as_raw_handle(),
                &mut adopted, PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, 0) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let ticket = format!("{} {seconds}\n{}\n{name}\n", duplicate as usize, adopted as usize);
            child.stdin.take().unwrap().write_all(ticket.as_bytes())?;
            super::super::group_windows::resume(child.id())?;
            let mut ready = [0];
            let mut output = child.stdout.take().unwrap();
            output.read_exact(&mut ready)?;
            #[cfg(test)]
            while ready != [1] {
                output.read_exact(&mut ready)?;
            }
            if ready != [1] {
                return Err(io::Error::other("holder not ready"));
            }
            Ok(())
        })();
        if setup.is_err() {
            let _ = child.kill();
            let _ = child.wait();
        } else {
            std::thread::spawn(move || {
                let _ = child.wait();
            });
        }
        setup.map(|()| no_breakaway)
    }
    #[cfg(test)]
    thread_local! { pub static FAIL_LAUNCH: std::cell::Cell<bool> = const { std::cell::Cell::new(false) }; }
    pub fn helper() -> i32 {
        let run = || -> io::Result<()> {
            if !cfg!(test) && std::env::args_os().count() != 2 {
                return Err(io::Error::other("invalid holder arguments"));
            }
            let mut ticket = String::new();
            std::io::stdin().take(1024).read_to_string(&mut ticket)?;
            let mut lines = ticket.lines();
            let (raw, seconds) = parse_ticket(lines.next().unwrap_or_default())
                .ok_or_else(|| io::Error::other("invalid holder ticket"))?;
            let event = lines
                .next()
                .and_then(|s| s.parse::<usize>().ok())
                .filter(|&h| h != 0)
                .ok_or_else(|| io::Error::other("invalid adoption handle"))?;
            // Ticket handles come only from the launching same-user daemon over its
            // private stdin pipe, via DuplicateHandle into this suspended helper.
            // The single-user Job DACL is the trust boundary, not the usize encoding.
            let mut owner = unsafe { OwnedHandle::from_raw_handle(event as _) };
            let name = lines
                .next()
                .ok_or_else(|| io::Error::other("missing Job name"))?;
            let event = adoption_event(name, false, true)?;
            let closing = adoption_event(name, true, true)?;
            let outcome = outcome_event(name, true)?;
            let rejected = rejected_event(name, true)?;
            let request = RequestSlot::open(name, true)?;
            let mut deadline = None;
            let mut admitted = false;
            let job = unsafe { OwnedHandle::from_raw_handle(raw as _) };
            std::io::stdout().write_all(&[1])?;
            // Readiness is binary, without a newline: flush before waiting for the
            // still-suspended payload, including when hosted by the test harness.
            std::io::stdout().flush()?;
            loop {
                if deadline.is_none()
                    && unsafe { WaitForSingleObject(owner.as_raw_handle(), 0) } == WAIT_OBJECT_0
                {
                    if admitted {
                        request.pid().store(0, std::sync::atomic::Ordering::SeqCst);
                        admitted = false;
                    }
                    deadline = Some(Instant::now() + Duration::from_secs(seconds));
                }
                let closing_now =
                    deadline.is_some_and(|d| Instant::now() + Duration::from_secs(6) >= d);
                if closing_now {
                    if unsafe { SetEvent(closing.as_raw_handle()) } == 0 {
                        return Err(io::Error::last_os_error());
                    }
                }
                // Admission and expiry are serialized here, not in the requester.
                if !closing_now
                    && unsafe { WaitForSingleObject(event.as_raw_handle(), 0) } == WAIT_OBJECT_0
                    && unsafe { WaitForSingleObject(rejected.as_raw_handle(), 0) } == WAIT_TIMEOUT
                {
                    let pid = request.pid().load(std::sync::atomic::Ordering::SeqCst);
                    if pid == 0
                        || pid >= u32::MAX - 1
                        || !crate::watch::windows::claim_adoption(request.pid(), pid)
                    {
                        continue;
                    }
                    let raw = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
                    if raw.is_null() {
                        if unsafe { ResetEvent(event.as_raw_handle()) } == 0 {
                            return Err(io::Error::last_os_error());
                        }
                        request.pid().store(0, std::sync::atomic::Ordering::SeqCst);
                        continue;
                    }
                    let next = unsafe { OwnedHandle::from_raw_handle(raw) };
                    owner = next;
                    if unsafe { ResetEvent(event.as_raw_handle()) } == 0 {
                        return Err(io::Error::last_os_error());
                    }
                    if unsafe { SetEvent(outcome.as_raw_handle()) } == 0 {
                        return Err(io::Error::last_os_error());
                    }
                    deadline = None;
                    admitted = true;
                }
                if deadline.is_some_and(|d| Instant::now() >= d) {
                    if unsafe { TerminateJobObject(job.as_raw_handle(), 1) } == 0 {
                        return Err(io::Error::last_os_error());
                    }
                    return Ok(());
                }
                let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
                if unsafe {
                    QueryInformationJobObject(
                        job.as_raw_handle(),
                        JobObjectBasicAccountingInformation,
                        (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                        std::mem::size_of_val(&info) as u32,
                        std::ptr::null_mut(),
                    )
                } == 0
                {
                    return Err(io::Error::last_os_error());
                }
                if info.ActiveProcesses == 0 {
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        };
        if run().is_ok() {
            0
        } else {
            1
        }
    }
}
#[cfg(test)]
#[rustfmt::skip]
mod tests {
    use super::*;
    #[test] fn bounds() {
        assert_eq!(validate_max_s(None), Ok(28800));
        for s in ["600", "172800"] { assert!(validate_max_s(Some(s)).is_ok()); }
        for s in ["599", "172801", "NaN", "inf", "-1", ""] { assert!(validate_max_s(Some(s)).is_err()); }
    }
    #[test] fn deadlines_and_tickets() {
        assert_eq!(deadline_ms(10, 600), Some(600010));
        assert_eq!(deadline_ms(u64::MAX, 1), None);
        assert_eq!(deadline_ms(1, u64::MAX), None);
        assert_eq!(parse_ticket("42 600010\n"), Some((42, 600010)));
        for s in ["", "0 1", "1 0", "1 2 3", "-1 2"] { assert!(parse_ticket(s).is_none()); }
    }
    #[test] fn restrictive_dacl() {
        assert_eq!(job_dacl("S-1-5-21-123"), Some("D:P(A;;GA;;;SY)(A;;GA;;;S-1-5-21-123)".into()));
        for s in ["", "S-1-5-", "S-1-5-21)(A;;GA;;;WD)"] { assert!(job_dacl(s).is_none()); }
    }
}
