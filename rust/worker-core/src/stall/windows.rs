//! Windows side of [`crate::stall`]: the supervised child and every descendant
//! run in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so terminating
//! or simply closing the job leaves nothing behind. The child starts suspended
//! and joins the job before its main thread is resumed, so no process can run
//! outside the job's kill reach.

use std::io;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, ExitStatus};

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    OpenProcess, OpenThread, ResumeThread, CREATE_SUSPENDED, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    THREAD_SUSPEND_RESUME,
};

/// Job object holding the supervised child and its descendants.
pub(super) struct Group {
    job: Job,
}

/// RAII job handle: dropping it closes the job, and closing kills whatever is
/// still inside (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), so every error path
/// leaks nothing and leaves nothing running.
struct Job(HANDLE);

impl Drop for Job {
    fn drop(&mut self) {
        // SAFETY: the handle is ours and closed exactly once here.
        unsafe {
            CloseHandle(self.0);
        }
    }
}

impl Group {
    /// Create the job, spawn `cmd` suspended, assign it to the job and only
    /// then resume its main thread. A failed assignment or resume terminates
    /// the child and returns the error; a failed spawn only drops the job.
    pub(super) fn spawn(cmd: &mut Command) -> io::Result<(Group, Child)> {
        let job = Job(create_job()?);
        cmd.creation_flags(CREATE_SUSPENDED);
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
        let pid = child.id();
        // The child is still suspended, so its pid cannot be reused yet and
        // this opens the process we just created.
        // SAFETY: OpenProcess with exactly the rights AssignProcessToJobObject
        // requires; the handle is closed again below on every path.
        let proc_handle = unsafe { OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid) };
        if proc_handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: both handles are open here and the job stays ours for good.
        let assigned = unsafe { AssignProcessToJobObject(self.job.0, proc_handle) };
        // SAFETY: our own open handle, closed exactly once here.
        unsafe {
            CloseHandle(proc_handle);
        }
        if assigned == 0 {
            return Err(io::Error::last_os_error());
        }
        resume_threads_of(pid)
    }

    /// Terminate every process in the job. Windows has no SIGTERM, so this is
    /// both the "term" and the "kill" step: `Opts::grace` is unused here.
    pub(super) fn terminate(&self) {
        self.terminate_job();
    }

    /// Terminate the job again: a sweep after the child exited (or after the
    /// grace elapsed) so no surviving grandchild holds the output pipe open.
    pub(super) fn kill(&self) {
        self.terminate_job();
    }

    fn terminate_job(&self) {
        // SAFETY: valid job handle; terminating a job without live processes
        // is a no-op and errors are best-effort.
        unsafe {
            TerminateJobObject(self.job.0, 1);
        }
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
        let e = io::Error::last_os_error();
        // SAFETY: our own open handle, closed exactly once here.
        unsafe {
            CloseHandle(job);
        }
        return Err(e);
    }
    Ok(job)
}

/// Resume every thread `pid` owns. A process spawned suspended has exactly its
/// main thread; resuming through a thread snapshot needs no thread handle from
/// the spawn itself.
fn resume_threads_of(pid: u32) -> io::Result<()> {
    // SAFETY: a thread-list snapshot; INVALID_HANDLE_VALUE signals failure.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
    let mut found = false;
    let mut result = Ok(());
    // SAFETY: snapshot is an open handle closed right below, and entry carries
    // its own size as Thread32First requires.
    unsafe {
        let mut more = Thread32First(snapshot, &mut entry);
        while more != 0 {
            if entry.th32OwnerProcessID == pid {
                found = true;
                if let Err(e) = resume_thread(entry.th32ThreadID) {
                    result = Err(e);
                    break;
                }
            }
            more = Thread32Next(snapshot, &mut entry);
        }
        CloseHandle(snapshot);
    }
    result?;
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
    // SAFETY: our own open handle, closed exactly once here.
    unsafe {
        CloseHandle(thread_handle);
    }
    if previous == u32::MAX {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::time::{Duration, Instant};
    use windows_sys::Win32::Foundation::STILL_ACTIVE;
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcess, GetExitCodeProcess, GetProcessHandleCount,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };

    use crate::stall::{supervise, supervise_with_cancel, Opts};

    /// Set only for helper runs; a plain `cargo test` sees it unset and skips.
    const HELPER_ENV: &str = "PG_STALL_TEST_HELPER";
    /// Directory for the helper's pid files (a fresh temp dir per test).
    const HELPER_DIR: &str = "PG_STALL_TEST_HELPER_DIR";

    fn quick() -> Opts {
        Opts {
            grace: Duration::from_millis(300),
            ..Opts::default()
        }
    }

    /// The test binary itself as the child (and grandchild): hermetic, no
    /// shell, no network. The name filter runs only `windows_supervise_helper`
    /// and `--no-capture` sends its prints straight to the output pipe.
    fn helper(mode: &str, dir: &Path) -> Command {
        let mut c = Command::new(std::env::current_exe().unwrap());
        c.arg("windows_supervise_helper")
            .arg("--no-capture")
            .env(HELPER_ENV, mode)
            .env(HELPER_DIR, dir);
        c
    }

    /// Started through [`helper`]: `sleep` just holds the pipe, `exit` exits
    /// with code 7, `leaf` publishes its pid and sleeps, and `parent` starts a
    /// `leaf` grandchild that inherits the pipe, publishes both pids and then
    /// goes silent so the stall watchdog fires.
    #[test]
    fn windows_supervise_helper() {
        let Ok(mode) = std::env::var(HELPER_ENV) else {
            return; // plain test run: nothing to do
        };
        let dir = std::env::var(HELPER_DIR).unwrap_or_default();
        match mode.as_str() {
            "sleep" => {}
            "exit" => std::process::exit(7),
            "leaf" => write_pid(&dir, "leaf.pid"),
            "parent" => {
                // The grandchild is killed with the job; waiting on it would
                // block until exactly that.
                #[allow(clippy::zombie_processes)]
                let _grandchild = helper("leaf", Path::new(&dir)).spawn().unwrap();
                let deadline = Instant::now() + Duration::from_secs(3);
                while !Path::new(&dir).join("leaf.pid").exists() {
                    assert!(Instant::now() < deadline, "grandchild never started");
                    println!("waiting"); // keeps the stall window open
                    std::thread::sleep(Duration::from_millis(50));
                }
                write_pid(&dir, "parent.pid");
                println!("ready"); // the last output: the stall window starts
            }
            other => panic!("unknown helper mode {other}"),
        }
        std::thread::sleep(Duration::from_secs(600));
    }

    /// Publish the helper's own pid for the test to inspect after the kill.
    fn write_pid(dir: &str, name: &str) {
        std::fs::write(Path::new(dir).join(name), std::process::id().to_string()).unwrap();
    }

    fn read_pid(dir: &Path, name: &str) -> u32 {
        std::fs::read_to_string(dir.join(name))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }

    fn process_alive(pid: u32) -> bool {
        // SAFETY: opens our own child's pid read-only; the handle is closed
        // again here on every path.
        unsafe {
            let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if h.is_null() {
                return false;
            }
            let mut code = 0u32;
            let ok = GetExitCodeProcess(h, &mut code);
            CloseHandle(h);
            ok != 0 && code == STILL_ACTIVE as u32
        }
    }

    fn wait_until_gone(pid: u32) -> bool {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if !process_alive(pid) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        false
    }

    fn handle_count() -> u32 {
        let mut n = 0u32;
        // SAFETY: the pseudo-handle of the running process; the count is
        // written into a local and the pseudo-handle is never closed.
        unsafe {
            GetProcessHandleCount(GetCurrentProcess(), &mut n);
        }
        n
    }

    #[test]
    fn stall_kill_takes_down_child_and_grandchild() {
        let d = tempfile::tempdir().unwrap();
        let o = Opts {
            stall: Some(Duration::from_millis(300)),
            timeout: Some(Duration::from_secs(3)),
            ..quick()
        };
        let t = Instant::now();
        let r = supervise(helper("parent", d.path()), &o).unwrap();
        assert!(r.stalled && !r.timed_out, "{r:?}");
        assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
        let parent = read_pid(d.path(), "parent.pid");
        let leaf = read_pid(d.path(), "leaf.pid");
        assert!(wait_until_gone(parent), "child {parent} still alive");
        assert!(wait_until_gone(leaf), "grandchild {leaf} still alive");
    }

    #[test]
    fn timeout_terminates_the_child() {
        let d = tempfile::tempdir().unwrap();
        let o = Opts {
            timeout: Some(Duration::from_millis(500)),
            ..quick()
        };
        let t = Instant::now();
        let r = supervise(helper("sleep", d.path()), &o).unwrap();
        assert!(r.timed_out && !r.stalled && !r.cancelled, "{r:?}");
        assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
    }

    #[test]
    fn cancel_terminates_the_child() {
        let d = tempfile::tempdir().unwrap();
        let flag = Arc::new(AtomicBool::new(false));
        let f = flag.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            f.store(true, Ordering::Relaxed);
        });
        let t = Instant::now();
        let r = supervise_with_cancel(helper("sleep", d.path()), &quick(), &|| {
            flag.load(Ordering::Relaxed)
        })
        .unwrap();
        assert!(r.cancelled && !r.stalled && !r.timed_out, "{r:?}");
        assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
    }

    #[test]
    fn normal_exit_reports_the_exit_code() {
        let d = tempfile::tempdir().unwrap();
        let r = supervise(helper("exit", d.path()), &quick()).unwrap();
        assert_eq!(r.code, Some(7));
        assert_eq!(r.signal, None);
        assert!(!r.stalled && !r.timed_out && !r.cancelled, "{r:?}");
    }

    #[test]
    fn failed_spawn_errors_without_leaking_a_job_handle() {
        let d = tempfile::tempdir().unwrap();
        let missing = Command::new(d.path().join("no-such-program.exe"));
        let before = handle_count();
        assert!(supervise(missing, &quick()).is_err());
        // Other tests run concurrently in this process, so accept any moment
        // where the count is back where it started.
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let now = handle_count();
            if now <= before {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "job handle leaked: {before} -> {now}"
            );
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}
