//! No-clobber forward/rollback moves; retain locks through the entire batch.
use std::{
    fs::{self, File},
    io,
    path::{Path, PathBuf},
};
#[derive(Default)]
pub(crate) struct Transaction {
    moves: Vec<Undo>,
    locks: Vec<File>,
}
struct Undo {
    old: PathBuf,
    new: PathBuf,
    source: bool,
    linked: bool,
}
impl Transaction {
    pub fn hold(&mut self, file: File) {
        self.locks.push(file);
    }
    pub fn record(&mut self, old: &Path, new: &Path, source: bool) -> usize {
        self.moves.push(Undo {
            old: old.into(),
            new: new.into(),
            source,
            linked: false,
        });
        self.moves.len() - 1
    }
    pub fn linked(&mut self, index: usize) {
        self.moves[index].linked = true;
    }
    pub fn rollback(&mut self) -> bool {
        let mut ok = true;
        for undo in self.moves.iter().rev() {
            if undo.linked {
                if fs::read_link(&undo.old).is_ok_and(|target| target == undo.new) {
                    if fs::remove_file(&undo.old).is_err() {
                        ok = false;
                        continue;
                    }
                } else {
                    ok = false;
                    continue;
                }
            }
            if undo.source && rename_new(&undo.new, &undo.old).is_err() {
                ok = false;
            }
        }
        self.moves.clear();
        ok
    }
}
pub(crate) fn rename_new(source: &Path, target: &Path) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use std::{ffi::CString, os::unix::ffi::OsStrExt};
        let source = CString::new(source.as_os_str().as_bytes())?;
        let target = CString::new(target.as_os_str().as_bytes())?;
        // SAFETY: both C strings live through the syscall; NOREPLACE forbids clobbering.
        if unsafe {
            libc::renameat2(
                libc::AT_FDCWD,
                source.as_ptr(),
                libc::AT_FDCWD,
                target.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        } != 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let source: Vec<u16> = source.as_os_str().encode_wide().chain([0]).collect();
        let target: Vec<u16> = target.as_os_str().encode_wide().chain([0]).collect();
        // SAFETY: terminated UTF-16 buffers live through the call; no REPLACE flag.
        if unsafe {
            windows_sys::Win32::Storage::FileSystem::MoveFileExW(
                source.as_ptr(),
                target.as_ptr(),
                0,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (source, target);
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "no-clobber rename unsupported",
        ))
    }
}
