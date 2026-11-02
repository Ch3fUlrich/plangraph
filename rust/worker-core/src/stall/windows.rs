//! Windows side of [`crate::stall`]: the supervised child and every descendant
//! run in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so terminating
//! or simply closing the job leaves nothing behind. The child starts suspended
//! and joins the job before its main thread is resumed, so no process can run
//! outside the job's kill reach.

use std::cell::Cell;
use std::io;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, ExitStatus};

use windows_sys::Win32::Foundation::{
    CloseHandle, ERROR_NO_MORE_FILES, HANDLE, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    OpenThread, ResumeThread, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
};

/// Job object holding the supervised child and its descendants.
pub(super) struct Group {
    job: Job,
}

/// RAII job handle: dropping it closes the job, and closing kills whatever is
/// still inside (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), so every error path
/// leaks nothing and leaves nothing running. A failing terminate closes the
/// handle early, so the cell nulls it out on close and closing twice is a
/// no-op.
struct Job(Cell<HANDLE>);

impl Job {
    /// The open handle (only valid until [`Job::close`]).
    fn handle(&self) -> HANDLE {
        self.0.get()
    }

    /// Close the handle now if it is still open. Closing kills whatever is
    /// still running in the job.
    fn close(&self) {
        let h = self.0.replace(std::ptr::null_mut());
        if !h.is_null() {
            // SAFETY: the handle is ours and closed exactly once here.
            unsafe {
                CloseHandle(h);
            }
        }
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        self.close();
    }
}

impl Group {
    /// Create the job, spawn `cmd` suspended, assign it to the job and only
    /// then resume its main thread. A failed assignment or resume terminates
    /// the child and returns the error; a failed spawn only drops the job.
    ///
    /// The creation flags belong to the supervisor and `cmd`'s own flags are
    /// **replaced** by `CREATE_SUSPENDED | CREATE_NO_WINDOW` (a supervised
    /// child is never interactive), so caller-set creation flags do not
    /// survive this call.
    pub(super) fn spawn(cmd: &mut Command) -> io::Result<(Group, Child)> {
        let job = Job(Cell::new(create_job()?));
        cmd.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW);
        let mut child = super::spawn_retrying(cmd)?;
        let group = Group { job };
        if let Err(e) = group.assign_and_resume(&child) {
            let _ = child.kill();
            return Err(e);
        }
        Ok((group, child))
    }

    /// Put the suspended child into the job, then let its main thread run.
    fn assign_and_resume(&self, child: &Child) -> io::Result<()> {
        // std's own process handle from CreateProcess already carries the
        // rights AssignProcessToJobObject needs, so no extra OpenProcess.
        // SAFETY: the handle is the child's own and stays owned by `child`.
        let assigned =
            unsafe { AssignProcessToJobObject(self.job.handle(), child.as_raw_handle()) };
        if assigned == 0 {
            // Captured right after the failing call, before any other API
            // call can overwrite the thread's last error.
            return Err(io::Error::last_os_error());
        }
        resume_threads_of(child.id())
    }

    /// Terminate every process in the job. Windows has no SIGTERM, so this is
    /// both the "term" and the "kill" step: `Opts::grace` is unused here.
    pub(super) fn terminate(&self) {
        // A failing terminate already closed the handle (below), so there is
        // nothing left to do here.
        let _ = self.terminate_job();
    }

    /// Terminate the job again: a sweep after the child exited (or after the
    /// grace elapsed) so no surviving grandchild holds the output pipe open.
    pub(super) fn kill(&self) {
        let _ = self.terminate_job();
    }

    /// Terminate the job and check the result. On failure the error is
    /// captured first, then the job handle is closed right away:
    /// `KILL_ON_JOB_CLOSE` then kills the group as a whole, so the reader
    /// thread can never block on a surviving grandchild.
    fn terminate_job(&self) -> io::Result<()> {
        // SAFETY: valid job handle while it is open; terminating a job
        // without live processes is a no-op.
        let ok = unsafe { TerminateJobObject(self.job.handle(), 1) };
        if ok == 0 {
            // Captured right after the failing call, before any CloseHandle.
            let e = io::Error::last_os_error();
            self.job.close();
            return Err(e);
        }
        Ok(())
    }
}

/// Windows reports kills as job terminations with an exit code, never as a
/// fatal signal.
pub(super) fn exit_signal(_status: &ExitStatus) -> Option<i32> {
    None
}

/// New job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` set.
fn create_job() -> io::Result<HANDLE> {
    // SAFETY: unnamed job with default security; a fresh kernel handle comes
    // back and is owned by the caller.
    let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if job.is_null() {
        return Err(io::Error::last_os_error());
    }
    let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    // SAFETY: job is open and ours; info is a zeroed struct plus the one flag
    // and is passed with its exact size.
    let ok = unsafe {
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &raw const info as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    if ok == 0 {
        // Captured right after the failing call, before any CloseHandle.
        let e = io::Error::last_os_error();
        // SAFETY: our own open handle, closed exactly once here.
        unsafe {
            CloseHandle(job);
        }
        return Err(e);
    }
    Ok(job)
}

/// Snapshot flag: list every thread on the system. The thread walk is declared
/// here directly against kernel32, with its own entry layout.
const TH32CS_SNAPTHREAD: u32 = 0x4;

/// One row of the thread snapshot; `dwSize` must be set to its size before the
/// walk.
#[repr(C)]
#[allow(non_snake_case, clippy::upper_case_acronyms)]
struct THREADENTRY32 {
    dwSize: u32,
    cntUsage: u32,
    th32ThreadID: u32,
    th32OwnerProcessID: u32,
    tpBasePri: i32,
    tpDeltaPri: i32,
    dwFlags: u32,
}

#[link(name = "kernel32")]
#[allow(non_snake_case)]
extern "system" {
    fn CreateToolhelp32Snapshot(dwflags: u32, th32processid: u32) -> HANDLE;
    fn Thread32First(hsnapshot: HANDLE, lpte: *mut THREADENTRY32) -> i32;
    fn Thread32Next(hsnapshot: HANDLE, lpte: *mut THREADENTRY32) -> i32;
}

/// Resume every thread `pid` owns. A process spawned suspended has exactly its
/// main thread; resuming through a thread snapshot needs no thread handle from
/// the spawn itself. The snapshot walks every thread on the system (`pid` is
/// filtered in userspace): a whole-system walk of a few milliseconds per
/// supervised spawn, accepted as is.
fn resume_threads_of(pid: u32) -> io::Result<()> {
    // SAFETY: a thread-list snapshot; INVALID_HANDLE_VALUE signals failure.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
    let mut found = false;
    let mut walk = Ok(());
    // SAFETY: snapshot is an open handle closed exactly once on every path
    // below, and entry carries its own size as the thread walk requires.
    unsafe {
        let mut more = Thread32First(snapshot, &mut entry);
        if more == 0 {
            // Captured before CloseHandle can overwrite the thread's error.
            let e = io::Error::last_os_error();
            CloseHandle(snapshot);
            if e.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
                return Err(e);
            }
            // ERROR_NO_MORE_FILES: the snapshot holds no threads at all; fall
            // through to the "no thread found" error below.
        } else {
            loop {
                if entry.th32OwnerProcessID == pid {
                    found = true;
                    if let Err(e) = resume_thread(entry.th32ThreadID) {
                        walk = Err(e);
                        break;
                    }
                }
                more = Thread32Next(snapshot, &mut entry);
                if more != 0 {
                    continue;
                }
                // A walk failure other than the end of the list is an error,
                // never "no threads": a broken snapshot must not read as an
                // empty one and kill an innocent child.
                let e = io::Error::last_os_error();
                if e.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
                    walk = Err(e);
                }
                break;
            }
            CloseHandle(snapshot);
        }
    }
    walk?;
    if found {
        Ok(())
    } else {
        Err(io::Error::other(format!(
            "no thread found for child pid {pid}"
        )))
    }
}

/// Resume one suspended thread of the child (its main thread at spawn time).
fn resume_thread(tid: u32) -> io::Result<()> {
    // SAFETY: opens our own child's thread with just the resume right.
    let thread_handle = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, tid) };
    if thread_handle.is_null() {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: valid thread handle on a thread CreateProcess left suspended.
    let previous = unsafe { ResumeThread(thread_handle) };
    // Captured right after the failing call, before any CloseHandle.
    let e = io::Error::last_os_error();
    // SAFETY: our own open handle, closed exactly once here.
    unsafe {
        CloseHandle(thread_handle);
    }
    if previous == u32::MAX {
        return Err(e);
    }
    Ok(())
}

#[cfg(test)]
mod tests;
