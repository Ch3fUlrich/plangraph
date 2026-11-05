//! Explicit, unwired durable ownership CAS. Unknown records are never replaced.
use crate::fleet_owner_lease::{Identity, Record, Refusal, State};
use crate::fleet_owner_transition::Transition;
use std::{io, path::Path};

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("slot busy")]
    Busy,
    #[error("stale epoch")]
    StaleEpoch,
    #[error("unknown: {0}")]
    Unknown(String),
    #[error("refused: {0:?}")]
    Refused(Refusal),
    #[error("io: {0}")]
    Io(#[from] io::Error),
    #[error("lock: {0}")]
    Lock(#[from] crate::lockfile::LockError),
}

#[allow(clippy::too_many_arguments)]
pub fn cas(
    root: &Path,
    slot: &Path,
    session: &str,
    run: &str,
    expected_epoch: u64,
    expected_identity: &State<Identity>,
    transition: Transition,
    now_s: i64,
) -> Result<Record, StoreError> {
    store(root, slot, |dir, name| {
        let record = match crate::fleet_owner_lease::read(dir, name, session, run, now_s) {
            crate::fleet_owner_lease::Read::Known(r) => r,
            crate::fleet_owner_lease::Read::Unknown { reason, .. } => {
                return Err(StoreError::Unknown(reason));
            }
        };
        if record.epoch != expected_epoch {
            return Err(StoreError::StaleEpoch);
        }
        crate::fleet_owner_transition::advance(
            &record,
            expected_epoch,
            expected_identity,
            transition.clone(),
            now_s,
        )
        .map_err(StoreError::Refused)
    })
}

pub fn create(
    root: &Path,
    slot: &Path,
    session: &str,
    run: &str,
    now_s: i64,
) -> Result<Record, StoreError> {
    store(root, slot, |dir, name| {
        match std::fs::symlink_metadata(dir.join(name)) {
            Ok(_) => return Err(io::Error::from(io::ErrorKind::AlreadyExists).into()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
        crate::fleet_owner_lease::bootstrap(session, run, now_s).map_err(StoreError::Refused)
    })
}

#[cfg(unix)]
fn store(
    root: &Path,
    slot: &Path,
    next: impl Fn(&Path, &Path) -> Result<Record, StoreError>,
) -> Result<Record, StoreError> {
    use std::{
        ffi::CString,
        fs::{File, OpenOptions},
        io::Write,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::{ffi::OsStrExt, fs::OpenOptionsExt},
        },
        sync::atomic::{AtomicU64, Ordering},
        time::{Duration, Instant},
    };
    crate::fleet_owner_lease::slot_path(root, slot).map_err(StoreError::Refused)?;
    if slot.components().any(|c| c.as_os_str() == ".owner-locks") {
        return Err(StoreError::Refused(Refusal::Invalid));
    }
    let mut parent = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY)
        .open(root)?;
    for part in slot.parent().unwrap().components() {
        let name = CString::new(part.as_os_str().as_bytes()).unwrap();
        // SAFETY: live directory descriptor and terminated component name.
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error().into());
        }
        parent = unsafe { File::from_raw_fd(fd) };
    }
    // The descriptor pins the parent, even if its original pathname is moved.
    let dir = std::path::PathBuf::from(format!("/dev/fd/{}/.", parent.as_raw_fd()));
    let name = Path::new(slot.file_name().unwrap());
    // Refusals must not materialize metadata; re-read after locking on success.
    next(&dir, name)?;
    let locks = dir.join(".owner-locks");
    match std::fs::create_dir(&locks) {
        Ok(()) => (),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
        Err(e) => return Err(e.into()),
    }
    let lock_dir = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY)
        .open(locks)?;
    let lock = Path::new(&format!("/dev/fd/{}/.", lock_dir.as_raw_fd())).join(name);
    // Reject lock symlinks too; create_new cannot follow a pre-existing link.
    match OpenOptions::new().write(true).create_new(true).open(&lock) {
        Ok(_) => (),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            OpenOptions::new()
                .write(true)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&lock)?;
        }
        Err(e) => return Err(e.into()),
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    let _guard = loop {
        if let Some(g) = crate::lockfile::try_lock(&lock)? {
            break g;
        }
        if Instant::now() >= deadline {
            return Err(StoreError::Busy);
        }
        std::thread::sleep(Duration::from_millis(1));
    };
    // Read through the existing confined reader, using the pinned parent.
    let record = next(&dir, name)?;
    static SERIAL: AtomicU64 = AtomicU64::new(0);
    let (temp, mut file) = loop {
        let id = SERIAL.fetch_add(1, Ordering::Relaxed);
        let temp = dir.join(format!(".owner-{}-{id}.tmp", std::process::id()));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&temp)
        {
            Ok(file) => break (temp, file),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e.into()),
        }
    };
    file.write_all(&serde_json::to_vec(&record).map_err(io::Error::other)?)?;
    file.sync_all()?;
    crash(1)?;
    std::fs::rename(&temp, dir.join(name))?;
    crash(2)?;
    parent.sync_all()?;
    Ok(record)
}

#[cfg(not(unix))]
fn store(
    _: &Path,
    _: &Path,
    _: impl FnOnce(&Path, &Path) -> Result<Record, StoreError>,
) -> Result<Record, StoreError> {
    Err(io::Error::from(io::ErrorKind::Unsupported).into())
}

fn crash(_point: u8) -> io::Result<()> {
    #[cfg(test)]
    if FAIL.with(|f| f.get() == _point) {
        return Err(io::Error::other("injected crash"));
    }
    Ok(())
}
#[cfg(test)]
thread_local! { static FAIL: std::cell::Cell<u8> = const { std::cell::Cell::new(0) }; }
#[cfg(all(test, unix))]
#[path = "fleet_owner_store_tests.rs"]
mod tests;
