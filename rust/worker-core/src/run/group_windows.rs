//! Named, kill-on-close execution groups. No caller integration until R6a-2e.
use super::group::*;
use std::{
    collections::HashMap,
    io, mem,
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
        process::CommandExt,
    },
    process::{Command, Stdio},
    ptr,
    sync::Mutex,
};
use windows_sys::Win32::{
    Foundation::*,
    System::{JobObjects::*, Threading::*},
};
// The thread walk reuses stall's own kernel32 declarations (no extra windows-sys feature).
use crate::stall::windows::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};

#[link(name = "kernel32")]
extern "system" {
    pub(super) fn SetLastError(error: u32);
    pub(super) fn IsProcessInJob(process: HANDLE, job: HANDLE, result: *mut i32) -> i32;
}

fn own(handle: HANDLE) -> io::Result<OwnedHandle> {
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful API result transfers a unique, closable handle.
    Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
}
pub struct JobGroup {
    job: OwnedHandle,
    identity: Identity,
    observed: Mutex<HashMap<u32, OwnedHandle>>,
    holder_deadline_ms: u64,
    holder_no_breakaway: bool,
}
pub fn spawn_grouped(
    unit: &str,
    spec: &LaunchSpec<'_>,
    _confinement: &Confinement,
    limits: &RunLimits,
) -> Result<Grouped, LaunchError> {
    validate_unit(unit)?;
    let name = job_name(unit);
    let wide: Vec<_> = std::ffi::OsStr::new(&name)
        .encode_wide()
        .chain(Some(0))
        .collect();
    // SAFETY: thread-local error slot and terminated name are valid.
    let job = super::job_holder_windows::secure_job(wide.as_ptr()).map_err(|e| {
        if e.raw_os_error() == Some(ERROR_ALREADY_EXISTS as i32) {
            LaunchError::JobExists
        } else {
            LaunchError::JobUnavailable
        }
    })?;
    let holder_deadline_ms = 0; // Determined by owner exit, never by launch.
                                // SAFETY: all-zero limit information is valid before setting the sole flag.
    let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { mem::zeroed() };
    // The independent holder retains this kill-on-close Job across daemon death.
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    // SAFETY: owned job and correctly sized, initialized information buffer.
    if unsafe {
        SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectExtendedLimitInformation,
            &info as *const _ as _,
            mem::size_of_val(&info) as u32,
        )
    } == 0
    {
        return Err(LaunchError::JobUnavailable);
    }
    // Open: deduplicate suspended-spawn/assign/resume shared with stall.
    let mut cmd = Command::new(spec.program);
    cmd.args(spec.args)
        .env_clear()
        .envs(spec.env.iter().cloned())
        .current_dir(spec.cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if spec.stdin_piped {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW);
    let mut child = cmd.spawn()?;
    let pid = child.id();
    let mut no_breakaway = false;
    let setup = (|| {
        // SAFETY: numeric child pid; requested rights suffice for assignment.
        let process = own(unsafe { OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid) })?;
        // SAFETY: both handles remain owned and open through this call.
        if unsafe { AssignProcessToJobObject(job.as_raw_handle(), process.as_raw_handle()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let holder = super::job_holder_windows::holder_path()?;
        no_breakaway = super::job_holder_windows::launch(
            &holder,
            &job,
            limits.windows_holder_max_s.unwrap_or(28800),
            &name,
        )?;
        resume(pid)
    })();
    if setup.is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(LaunchError::Io(setup.unwrap_err()));
    }
    let identity = Identity {
        pid,
        start_time: creation_time(pid),
        group: Some(name),
    };
    Ok(Grouped {
        child,
        group: Box::new(JobGroup {
            job,
            identity,
            observed: Mutex::new(HashMap::new()),
            holder_deadline_ms,
            holder_no_breakaway: no_breakaway,
        }),
    })
}
pub(super) fn creation_time(pid: u32) -> Option<u64> {
    // SAFETY: numeric pid, query-only access; ownership passes to RAII.
    let process = own(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) }).ok()?;
    let (mut created, mut exit, mut kernel, mut user) = (
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
    );
    // SAFETY: open process and four writable FILETIME buffers.
    if unsafe {
        GetProcessTimes(
            process.as_raw_handle(),
            &mut created,
            &mut exit,
            &mut kernel,
            &mut user,
        )
    } == 0
    {
        return None;
    }
    Some((u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime))
}
pub(super) fn resume(pid: u32) -> io::Result<()> {
    // Child exposes no main-thread handle, so std spawning requires a host-wide snapshot.
    // SAFETY: thread snapshot API has no pointer inputs; RAII closes the result.
    let snapshot = own(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) })?;
    // SAFETY: zero is valid initialization; dwSize is set before enumeration.
    let mut entry: THREADENTRY32 = unsafe { mem::zeroed() };
    entry.dwSize = mem::size_of_val(&entry) as u32;
    let mut resumed = false;
    // SAFETY: valid snapshot and correctly sized writable entry.
    let mut more = unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) };
    loop {
        if more == 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
                return Err(error);
            }
            break;
        }
        if entry.th32OwnerProcessID == pid {
            // SAFETY: thread id came from the snapshot; minimum required right.
            let thread = own(unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) })?;
            // SAFETY: owned thread handle with resume permission.
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            resumed = true;
        }
        // SAFETY: snapshot remains open and entry remains correctly sized.
        more = unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) };
    }
    if resumed {
        Ok(())
    } else {
        Err(io::Error::other("no child thread resumed"))
    }
}
impl RunGroup for JobGroup {
    fn holder_deadline_ms(&self) -> Option<u64> {
        (self.holder_deadline_ms != 0).then_some(self.holder_deadline_ms)
    }
    fn holder_no_breakaway(&self) -> bool {
        self.holder_no_breakaway
    }
    fn identity(&self) -> &Identity {
        &self.identity
    }
    fn members(&self) -> io::Result<Vec<u32>> {
        let mut observed = self
            .observed
            .lock()
            .map_err(|_| io::Error::other("job process tracking lock poisoned"))?;
        let mut capacity = 1024;
        for _ in 0..4 {
            // usize storage supplies the alignment required by the pid-list header.
            let mut buffer = vec![0usize; capacity + 2];
            // SAFETY: aligned buffer has room for the two DWORDs and capacity pids.
            let ok = unsafe {
                QueryInformationJobObject(
                    self.job.as_raw_handle(),
                    JobObjectBasicProcessIdList,
                    buffer.as_mut_ptr().cast(),
                    (buffer.len() * mem::size_of::<usize>()) as u32,
                    ptr::null_mut(),
                )
            };
            let error = io::Error::last_os_error();
            // SAFETY: query initialized the header in our aligned buffer.
            let list = unsafe { &*buffer.as_ptr().cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>() };
            if ok == 0 && error.raw_os_error() != Some(ERROR_MORE_DATA as i32) {
                return Err(error);
            }
            if list.NumberOfAssignedProcesses > list.NumberOfProcessIdsInList {
                capacity = list.NumberOfAssignedProcesses as usize + 16;
                continue;
            }
            if ok == 0 {
                return Err(error);
            }
            let count = list.NumberOfProcessIdsInList as usize;
            if count > capacity {
                return Err(io::Error::other("invalid job pid count"));
            }
            // SAFETY: successful query supplied count pids within the allocated buffer.
            let pids = unsafe { std::slice::from_raw_parts(list.ProcessIdList.as_ptr(), count) };
            let mut members: Vec<u32> = pids.iter().map(|&p| p as u32).collect();
            for &pid in &members {
                if observed.contains_key(&pid) {
                    continue;
                }
                // SAFETY: numeric pid; query and wait rights only, owned on success.
                let Ok(process) = own(unsafe {
                    OpenProcess(
                        PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                        0,
                        pid,
                    )
                }) else {
                    // The job still lists this pid even if opening it races with exit.
                    continue;
                };
                let mut inside = 0;
                // SAFETY: both handles are open and inside is writable. Verify the
                // process belongs to this job before retaining a possibly reused pid.
                if unsafe {
                    IsProcessInJob(
                        process.as_raw_handle(),
                        self.job.as_raw_handle(),
                        &mut inside,
                    )
                } == 0
                {
                    return Err(io::Error::last_os_error());
                }
                if inside != 0 {
                    observed.insert(pid, process);
                }
            }
            let mut wait_error = None;
            observed.retain(|&pid, process| {
                // SAFETY: retained process handle has synchronization access.
                match unsafe { WaitForSingleObject(process.as_raw_handle(), 0) } {
                    WAIT_OBJECT_0 => false,
                    WAIT_FAILED => {
                        wait_error = Some(io::Error::last_os_error());
                        true
                    }
                    _ => {
                        if !members.contains(&pid) {
                            members.push(pid);
                        }
                        true
                    }
                }
            });
            if let Some(error) = wait_error {
                return Err(error);
            }
            return Ok(members);
        }
        Err(io::Error::from_raw_os_error(ERROR_MORE_DATA as i32))
    }
    fn terminate(&self) {
        // SAFETY: owned job stays open; Windows has no polite termination signal.
        unsafe {
            TerminateJobObject(self.job.as_raw_handle(), 1);
        }
    }
    fn kill(&self) {
        self.terminate();
    }
}

impl Drop for JobGroup {
    fn drop(&mut self) {
        // Normal teardown is not daemon death: explicitly end remaining payloads.
        self.terminate();
    }
}
