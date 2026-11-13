use super::rename::{self, Step};
use std::{io, mem};
use windows_sys::Win32::Storage::FileSystem::FILE_RENAME_INFO;
#[cfg(windows)]
use windows_sys::Win32::Storage::FileSystem::{
    FileRenameInfo, FileRenameInfoEx, MoveFileExW, SetFileInformationByHandle,
    MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};
#[cfg(windows)]
use {
    super::rename::wide,
    std::{os::windows::io::AsRawHandle, path::Path},
};

#[cfg(windows)]
pub(super) fn replace(file: &tempfile::NamedTempFile, path: &Path, step: Step) -> io::Result<()> {
    let name = wide(path);
    if step == Step::Move {
        let source = wide(file.path());
        // SAFETY: live NUL-terminated names in the same directory. No COPY_ALLOWED.
        return result(unsafe {
            MoveFileExW(
                source.as_ptr(),
                name.as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        });
    }
    let (storage, size) = rename_info(&name, step)?;
    let class = if step == Step::Legacy {
        FileRenameInfo
    } else {
        FileRenameInfoEx
    };
    // SAFETY: rename_info covers all fields and UTF-16+NUL;
    // handle has DELETE, target is absolute and remains on the same volume.
    unsafe {
        result(SetFileInformationByHandle(
            file.as_raw_handle(),
            class,
            storage.as_ptr().cast(),
            size,
        ))
    }
}

// Shared with Linux tests: only the final OS calls above are Windows-only.
fn rename_info(name: &[u16], step: Step) -> io::Result<(Vec<usize>, u32)> {
    let offset = mem::offset_of!(FILE_RENAME_INFO, FileName);
    let size = rename::buffer_size(offset, name.len() - 1)?;
    let name_bytes =
        u32::try_from((name.len() - 1) * 2).map_err(|_| io::ErrorKind::InvalidInput)?;
    // sizeof includes the one-element FileName and tail padding. Allocate it
    // for typed field access, but pass only the field offset plus the name.
    let allocation = (size as usize).max(mem::size_of::<FILE_RENAME_INFO>());
    let mut storage = vec![0usize; allocation.div_ceil(mem::size_of::<usize>())];
    let info = storage.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    // SAFETY: aligned, initialized storage covers all fields and UTF-16+NUL.
    unsafe {
        (*info).RootDirectory = std::ptr::null_mut();
        (*info).FileNameLength = name_bytes;
        match step {
            Step::Posix => (*info).Anonymous.Flags = 0x1 | 0x2,
            Step::Replace => (*info).Anonymous.Flags = 0x1,
            // windows-sys 0.61.2 declares this member as bool, not u8.
            Step::Legacy => (*info).Anonymous.ReplaceIfExists = true,
            Step::Move => unreachable!(),
        }
        std::ptr::copy_nonoverlapping(
            name.as_ptr(),
            storage.as_mut_ptr().cast::<u8>().add(offset).cast::<u16>(),
            name.len(),
        );
    }
    Ok((storage, size))
}

#[cfg(windows)]
fn result(value: i32) -> io::Result<()> {
    if value == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(test)]
#[test]
fn production_rename_buffer_encodes_every_handle_step() {
    for name in ["a", "C:\\status.json", "C:\\status-\u{1f980}.json"] {
        let wide: Vec<_> = name.encode_utf16().chain(Some(0)).collect();
        for step in [Step::Posix, Step::Replace, Step::Legacy] {
            let (storage, size) = rename_info(&wide, step).unwrap();
            let offset = mem::offset_of!(FILE_RENAME_INFO, FileName);
            assert_eq!(size as usize, offset + wide.len() * 2);
            assert!(storage.len() * mem::size_of::<usize>() >= mem::size_of::<FILE_RENAME_INFO>());
            // SAFETY: the production builder allocates the complete aligned header
            // and name; inspect that same storage, not a synthetic layout.
            unsafe {
                let info = &*storage.as_ptr().cast::<FILE_RENAME_INFO>();
                assert!(info.RootDirectory.is_null());
                assert_eq!(info.FileNameLength as usize, (wide.len() - 1) * 2);
                if step == Step::Legacy {
                    let replace: bool = info.Anonymous.ReplaceIfExists;
                    assert!(replace);
                } else {
                    assert_eq!(
                        info.Anonymous.Flags,
                        if step == Step::Posix { 3 } else { 1 }
                    );
                }
                let tail = storage.as_ptr().cast::<u8>().add(offset).cast::<u16>();
                assert_eq!(std::slice::from_raw_parts(tail, wide.len()), wide);
            }
        }
    }
}
