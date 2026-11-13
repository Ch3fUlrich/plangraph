use super::rename::{self, wide, Step};
use std::{io, mem, os::windows::io::AsRawHandle, path::Path};
use windows_sys::Win32::Storage::FileSystem::{
    FileRenameInfo, FileRenameInfoEx, MoveFileExW, SetFileInformationByHandle, FILE_RENAME_INFO,
    MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};

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
    let offset = mem::offset_of!(FILE_RENAME_INFO, FileName);
    let size = rename::buffer_size(offset, name.len() - 1)?;
    let name_bytes =
        u32::try_from((name.len() - 1) * 2).map_err(|_| io::ErrorKind::InvalidInput)?;
    // Allocate a full header for typed field access, but pass only offset+tail
    // to Windows: sizeof includes the one-element FileName and tail padding.
    let allocation = (size as usize).max(mem::size_of::<FILE_RENAME_INFO>());
    let mut storage = vec![0usize; allocation.div_ceil(mem::size_of::<usize>())];
    let info = storage.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    let class = if step == Step::Legacy {
        FileRenameInfo
    } else {
        FileRenameInfoEx
    };
    // SAFETY: aligned, initialized storage covers all fields and UTF-16+NUL;
    // handle has DELETE, target is absolute and remains on the same volume.
    unsafe {
        (*info).RootDirectory = std::ptr::null_mut();
        (*info).FileNameLength = name_bytes;
        match step {
            Step::Posix => (*info).Anonymous.Flags = 0x1 | 0x2,
            Step::Replace => (*info).Anonymous.Flags = 0x1,
            Step::Legacy => (*info).Anonymous.ReplaceIfExists = true,
            Step::Move => unreachable!(),
        }
        std::ptr::copy_nonoverlapping(
            name.as_ptr(),
            storage.as_mut_ptr().cast::<u8>().add(offset).cast::<u16>(),
            name.len(),
        );
        result(SetFileInformationByHandle(
            file.as_raw_handle(),
            class,
            info.cast(),
            size,
        ))
    }
}

fn result(value: i32) -> io::Result<()> {
    if value == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
