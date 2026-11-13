//! Bounded, no-follow reconciliation JSON reader.
use super::{helpers::check, ReconcileError};
use serde_json::Value;
use std::{io::Read, path::Path};

pub(crate) fn read(path: &Path) -> Result<Value, ReconcileError> {
    check(path, false)?;
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path).map_err(|_| ReconcileError::Io)?;
    let meta = file.metadata().map_err(|_| ReconcileError::Io)?;
    const CAP: u64 = 8 * 1024 * 1024;
    if !meta.is_file() || meta.len() > CAP {
        return Err(ReconcileError::Invalid);
    }
    let mut bytes = Vec::new();
    file.take(CAP + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ReconcileError::Io)?;
    if bytes.len() as u64 > CAP {
        return Err(ReconcileError::Invalid);
    }
    serde_json::from_slice(&bytes).map_err(|_| ReconcileError::Invalid)
}
