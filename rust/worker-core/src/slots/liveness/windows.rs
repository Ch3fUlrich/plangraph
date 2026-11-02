//! Windows probes: Job Objects for groups, process times for identity, and a
//! share-mode-0 open for seat index locks (compile-checked, D-3705).

use super::{GroupLiveness, GroupState, SeatLocks};
use std::path::{Path, PathBuf};

/// Group liveness from the run's Job Object and process identity from the
/// process creation FILETIME (compared for equality only).
#[derive(Debug, Default, Clone, Copy)]
pub struct JobLiveness;

impl GroupLiveness for JobLiveness {
    fn group_state(&self, group: &str) -> GroupState {
        use windows_sys::Win32::Foundation::{
            CloseHandle, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND,
        };
        use windows_sys::Win32::System::JobObjects::OpenJobObjectW;
        use windows_sys::Win32::System::SystemServices::JOB_OBJECT_QUERY;

        let name: Vec<u16> = group.encode_utf16().chain(std::iter::once(0)).collect();
        // SAFETY: `name` is a NUL-terminated UTF-16 string; the returned
        // handle is checked for null and closed on every path below.
        let job = unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, 0, name.as_ptr()) };
        if job.is_null() {
            return match std::io::Error::last_os_error().raw_os_error() {
                Some(e) if e == ERROR_FILE_NOT_FOUND as i32 || e == ERROR_PATH_NOT_FOUND as i32 => {
                    GroupState::Gone
                }
                _ => GroupState::Unknown,
            };
        }
        let state = job_state(job);
        // SAFETY: `job` is an open handle owned by this function.
        unsafe { CloseHandle(job) };
        state
    }

    fn process_alive(&self, pid: u32, start_time: u64) -> bool {
        use windows_sys::Win32::Foundation::{CloseHandle, FILETIME, STILL_ACTIVE};
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        // SAFETY: `pid` is a plain id; the returned handle is checked for null
        // and closed on every path below.
        let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if process.is_null() {
            return false;
        }
        let mut creation = FILETIME::default();
        let mut exit = FILETIME::default();
        let mut kernel = FILETIME::default();
        let mut user = FILETIME::default();
        // SAFETY: all out-params are stack values that outlive the call.
        let times_ok =
            unsafe { GetProcessTimes(process, &mut creation, &mut exit, &mut kernel, &mut user) };
        let mut exit_code: u32 = 0;
        // SAFETY: `process` is open and `exit_code` outlives the call.
        let code_ok = unsafe { GetExitCodeProcess(process, &mut exit_code) };
        // SAFETY: `process` is an open handle owned by this function.
        unsafe { CloseHandle(process) };
        let created = ((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64;
        times_ok != 0 && code_ok != 0 && created == start_time && exit_code == STILL_ACTIVE as u32
    }
}

/// `Live(n)`/`Empty` from `ActiveProcesses`, `Unknown` when the query fails.
fn job_state(job: windows_sys::Win32::Foundation::HANDLE) -> GroupState {
    use windows_sys::Win32::System::JobObjects::{
        JobObjectBasicAccountingInformation, QueryInformationJobObject,
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    };

    let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    // SAFETY: `job` is an open handle; `info` is a stack value that outlives
    // the call and is fully initialized by the kernel on success.
    let ok = unsafe {
        QueryInformationJobObject(
            job,
            JobObjectBasicAccountingInformation,
            &mut info as *mut _ as *mut core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return GroupState::Unknown;
    }
    if info.ActiveProcesses == 0 {
        GroupState::Empty
    } else {
        GroupState::Live(info.ActiveProcesses)
    }
}

/// Seat index locks checked with a share-mode-0 open: a sharing violation
/// means a live process holds the file.
pub struct FileSeatLocks<F> {
    map: F,
}

impl<F> FileSeatLocks<F> {
    /// The caller-supplied `repo_id` -> repo root map (R4/R5 own seat
    /// worktrees).
    pub fn new(map: F) -> Self {
        FileSeatLocks { map }
    }
}

impl<F: Fn(&str) -> Option<PathBuf>> SeatLocks for FileSeatLocks<F> {
    fn index_lock(&self, repo_id: &str, seat_slot: u32) -> Option<PathBuf> {
        let root = (self.map)(repo_id)?;
        Some(super::seat_index_lock(&root, seat_slot))
    }

    fn held_by_live_process(&self, path: &Path) -> bool {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::{
            CloseHandle, ERROR_FILE_NOT_FOUND, ERROR_LOCK_VIOLATION, ERROR_PATH_NOT_FOUND,
            ERROR_SHARING_VIOLATION, INVALID_HANDLE_VALUE,
        };
        use windows_sys::Win32::Storage::FileSystem::{
            CreateFileW, FILE_ATTRIBUTE_NORMAL, OPEN_EXISTING,
        };

        let name: Vec<u16> = path
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        // SAFETY: `name` is a NUL-terminated UTF-16 string; desired access 0
        // asks only for existence and share mode 0 demands no other open
        // handle. The returned handle is closed below when open succeeded.
        let file = unsafe {
            CreateFileW(
                name.as_ptr(),
                0,
                0,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            )
        };
        if file != INVALID_HANDLE_VALUE {
            // SAFETY: `file` is an open handle owned by this function.
            unsafe { CloseHandle(file) };
            return false;
        }
        match std::io::Error::last_os_error().raw_os_error() {
            Some(e) if e == ERROR_SHARING_VIOLATION as i32 || e == ERROR_LOCK_VIOLATION as i32 => {
                true
            }
            Some(e) if e == ERROR_FILE_NOT_FOUND as i32 || e == ERROR_PATH_NOT_FOUND as i32 => {
                false
            }
            // Unreadable state: fail safe and leave the file alone.
            _ => true,
        }
    }
}
