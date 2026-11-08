//! Recorded Job adoption; no spawn, pipe or waitpid authority.
use crate::cpu::CpuSample;
use std::io;
#[cfg(any(windows, test))]
pub(crate) fn claim_adoption(slot: &std::sync::atomic::AtomicU32, pid: u32) -> bool {
    slot.compare_exchange(
        pid,
        u32::MAX,
        std::sync::atomic::Ordering::SeqCst,
        std::sync::atomic::Ordering::SeqCst,
    )
    .is_ok()
}

pub trait JobApi: Send {
    fn adopted(&mut self) -> io::Result<()> {
        Ok(())
    }
    fn member(&mut self, pid: u32, start: u64) -> io::Result<bool>;
    fn active(&mut self) -> io::Result<bool>;
    fn terminate(&mut self) -> io::Result<()>;
    fn times(&mut self) -> io::Result<(i64, i64)>;
}
pub struct AdoptedJob<A> {
    api: Option<A>,
    gone: bool,
}
impl<A: JobApi> AdoptedJob<A> {
    pub fn verify(mut api: Option<A>, pid: u32, start: u64) -> io::Result<Self> {
        let gone = match api.as_mut() {
            Some(api) => !api.member(pid, start)?,
            None => true,
        };
        Ok(Self { api, gone })
    }
    pub fn adopt(&mut self) -> io::Result<()> {
        if !self.is_gone()? {
            self.api.as_mut().unwrap().adopted()?;
        }
        Ok(())
    }
    pub fn cpu(&mut self) -> io::Result<CpuSample> {
        if self.empty()? {
            return Err(io::ErrorKind::NotFound.into());
        }
        let (user, kernel) = self.api.as_mut().unwrap().times()?;
        let total = u64::try_from(user)
            .ok()
            .and_then(|u| u64::try_from(kernel).ok().and_then(|k| u.checked_add(k)))
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
        Ok(CpuSample {
            total_ms: total / 10_000,
        })
    }
    pub fn empty(&mut self) -> io::Result<bool> {
        match self.api.as_mut() {
            Some(api) => Ok(!api.active()?),
            None => Ok(true),
        }
    }
    pub fn is_gone(&mut self) -> io::Result<bool> {
        Ok(self.gone || self.empty()?)
    }
    pub fn terminate(&mut self) -> io::Result<()> {
        if let Some(api) = self.api.as_mut() {
            api.terminate()?;
        }
        Ok(())
    }
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::{
        mem,
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::*,
        System::{
            JobObjects::*,
            SystemServices::{JOB_OBJECT_QUERY, JOB_OBJECT_TERMINATE},
            Threading::*,
        },
    };
    pub struct NativeJob(OwnedHandle, String);
    impl NativeJob {
        pub fn open(name: &str) -> io::Result<Option<Self>> {
            let name: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
            // SAFETY: terminated name, non-inheritable handle, minimum rights.
            let raw = unsafe {
                OpenJobObjectW(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, 0, name.as_ptr())
            };
            if raw.is_null() {
                let error = io::Error::last_os_error();
                return if error.raw_os_error() == Some(ERROR_FILE_NOT_FOUND as i32) {
                    Ok(None)
                } else {
                    Err(error)
                };
            }
            // SAFETY: successful open transfers an owned handle.
            let job = unsafe { OwnedHandle::from_raw_handle(raw) };
            let name = String::from_utf16_lossy(&name[..name.len() - 1]);
            Ok(Some(Self(job, name)))
        }

        fn accounting(&self) -> io::Result<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION> {
            // SAFETY: zero-initialized output and exact buffer size.
            let mut info = unsafe { mem::zeroed::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() };
            if unsafe {
                QueryInformationJobObject(
                    self.0.as_raw_handle(),
                    JobObjectBasicAccountingInformation,
                    (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    mem::size_of_val(&info) as u32,
                    ptr::null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(info)
        }
    }
    impl JobApi for NativeJob {
        fn adopted(&mut self) -> io::Result<()> {
            use crate::run::job_holder_windows::{
                adoption_event, outcome_event, rejected_event, RequestSlot,
            };
            let event = adoption_event(&self.1, false, false)?;
            let closing = adoption_event(&self.1, true, false)?;
            let outcome = outcome_event(&self.1, false)?;
            let rejected = rejected_event(&self.1, false)?;
            let request = RequestSlot::open(&self.1, false)?;
            if unsafe { WaitForSingleObject(closing.as_raw_handle(), 0) } != WAIT_TIMEOUT {
                return Err(io::Error::other("holder deadline within adoption margin"));
            }
            request
                .pid()
                .compare_exchange(
                    0,
                    unsafe { GetCurrentProcessId() },
                    std::sync::atomic::Ordering::SeqCst,
                    std::sync::atomic::Ordering::SeqCst,
                )
                .map_err(|_| io::Error::other("holder adoption already requested"))?;
            let result = if unsafe { ResetEvent(outcome.as_raw_handle()) } == 0
                || unsafe { ResetEvent(rejected.as_raw_handle()) } == 0
                || unsafe { SetEvent(event.as_raw_handle()) } == 0
            {
                Err(io::Error::last_os_error())
            } else if unsafe { WaitForSingleObject(outcome.as_raw_handle(), 5000) } == WAIT_OBJECT_0
            {
                Ok(())
            } else {
                Err(io::Error::other("holder did not acknowledge adoption"))
            };
            // Latch cancellation even when event operations failed. Once claimed,
            // wait for acknowledgement before releasing the slot for the next exchange.
            let committed = request.pid().compare_exchange(
                unsafe { GetCurrentProcessId() },
                u32::MAX - 1,
                std::sync::atomic::Ordering::SeqCst,
                std::sync::atomic::Ordering::SeqCst,
            ) == Err(u32::MAX);
            if committed {
                while unsafe { WaitForSingleObject(outcome.as_raw_handle(), 1000) } != WAIT_OBJECT_0
                {
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
            }
            let mut cleanup_error = None;
            for handle in [&event, &rejected] {
                if unsafe { ResetEvent(handle.as_raw_handle()) } == 0 {
                    cleanup_error = Some(io::Error::last_os_error());
                }
            }
            request.pid().store(0, std::sync::atomic::Ordering::SeqCst);
            if committed {
                Ok(())
            } else {
                cleanup_error.map_or(result, Err)
            }
        }
        fn member(&mut self, pid: u32, start: u64) -> io::Result<bool> {
            // SAFETY: query-only process handle; retained through identity/membership checks.
            let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
            if raw.is_null() {
                let error = io::Error::last_os_error();
                return if error.raw_os_error() == Some(ERROR_INVALID_PARAMETER as i32) {
                    Ok(false)
                } else {
                    Err(error)
                };
            }
            // SAFETY: successful open transfers ownership.
            let process = unsafe { OwnedHandle::from_raw_handle(raw) };
            let (mut created, mut exit, mut kernel, mut user) = (
                FILETIME::default(),
                FILETIME::default(),
                FILETIME::default(),
                FILETIME::default(),
            );
            let mut inside = 0;
            // SAFETY: live owned handles and writable output buffers.
            if unsafe {
                GetProcessTimes(
                    process.as_raw_handle(),
                    &mut created,
                    &mut exit,
                    &mut kernel,
                    &mut user,
                )
            } == 0
                || unsafe {
                    IsProcessInJob(process.as_raw_handle(), self.0.as_raw_handle(), &mut inside)
                } == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(inside != 0
                && (u64::from(created.dwHighDateTime) << 32 | u64::from(created.dwLowDateTime))
                    == start)
        }
        fn active(&mut self) -> io::Result<bool> {
            Ok(self.accounting()?.ActiveProcesses != 0)
        }
        fn times(&mut self) -> io::Result<(i64, i64)> {
            let info = self.accounting()?;
            Ok((info.TotalUserTime, info.TotalKernelTime))
        }
        fn terminate(&mut self) -> io::Result<()> {
            // SAFETY: only the verified, retained Job handle is targeted.
            if unsafe { TerminateJobObject(self.0.as_raw_handle(), 1) } == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
    }
}
#[cfg(windows)]
pub use native::NativeJob;
#[cfg(test)]
#[path = "watch_windows_tests.rs"]
mod tests;
