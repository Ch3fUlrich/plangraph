//! The bounded reader of one run record file, shared by the MCP service and the admission guard.
use serde_json::Value;
use std::{io::Read, path::Path};

pub const MAX_RECORD_BYTES: u64 = 8 * 1024 * 1024;

/// Why a record file gave no value: it is not there (a run that vanished meanwhile), or it is
/// there and cannot be trusted (not a regular file, too large, unreadable, not JSON).
#[derive(Debug, PartialEq, Eq)]
pub enum ReadError {
    Missing,
    Invalid,
}

/// Open a regular file without following a link or blocking on a pipe.
pub fn open_record(path: &Path) -> std::io::Result<std::fs::File> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x00200000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(std::io::Error::other("invalid run record"));
        }
    }
    if !file.metadata()?.is_file() {
        return Err(std::io::Error::other("invalid run record"));
    }
    Ok(file)
}

/// The JSON value of a record file, read within `MAX_RECORD_BYTES`.
pub fn read_json(path: &Path) -> Result<Value, ReadError> {
    let read = || -> std::io::Result<Vec<u8>> {
        let metadata = std::fs::symlink_metadata(path)?;
        if !metadata.is_file() || metadata.len() > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("invalid run record"));
        }
        let mut bytes = Vec::new();
        open_record(path)?
            .take(MAX_RECORD_BYTES + 1)
            .read_to_end(&mut bytes)?;
        Ok(bytes)
    };
    let bytes = read().map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            ReadError::Missing
        } else {
            ReadError::Invalid
        }
    })?;
    if bytes.len() as u64 > MAX_RECORD_BYTES {
        return Err(ReadError::Invalid);
    }
    serde_json::from_slice(&bytes).map_err(|_| ReadError::Invalid)
}
