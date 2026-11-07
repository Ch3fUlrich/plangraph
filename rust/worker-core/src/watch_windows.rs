//! Recorded Job adoption; no spawn, pipe or waitpid authority.
use crate::cpu::CpuSample;
use std::io;

pub trait JobApi: Send {
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
        if !self.gone {
            self.gone = !self.api.as_mut().unwrap().active()?;
        }
        Ok(self.gone)
    }
    pub fn terminate(&mut self) -> io::Result<()> {
        if !self.empty()? {
            self.api.as_mut().unwrap().terminate()?;
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
    pub struct NativeJob(OwnedHandle);
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
            Ok(Some(Self(unsafe { OwnedHandle::from_raw_handle(raw) })))
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
