//! File-only credential boundary for the pending HTTP transport.
use crate::io::Error;
use std::{fs::File, io::Read, path::Path};

/// Never includes the credential or filesystem path in an error or Debug output.
/// Non-Unix hosts fail closed until equivalent ACL validation is available.
pub fn read_token(path: &Path) -> Result<String, Error> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Err(Error::Outage)
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let file = File::open(path).map_err(|_| Error::Outage)?;
        let metadata = file.metadata().map_err(|_| Error::Outage)?;
        if !metadata.is_file() || metadata.permissions().mode() & 0o177 != 0 {
            return Err(Error::Outage);
        }
        let mut token = String::new();
        file.take(16385)
            .read_to_string(&mut token)
            .map_err(|_| Error::Outage)?;
        if token.len() > 16384 {
            return Err(Error::Outage);
        }
        let token = token.trim();
        if token.is_empty() || token.bytes().any(|b| b.is_ascii_control()) {
            return Err(Error::Outage);
        }
        Ok(token.to_owned())
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{
        io::Write,
        os::unix::fs::{OpenOptionsExt, PermissionsExt},
    };
    #[test]
    fn token_file_modes_and_content_fail_closed() {
        let path = std::env::temp_dir().join(format!("og-memory-token-{}", std::process::id()));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        struct Cleanup(std::path::PathBuf);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = std::fs::remove_file(&self.0);
            }
        }
        let _cleanup = Cleanup(path.clone());
        // Generate an ephemeral local-only value; no credential fixtures or logs.
        let value = format!("local-{}", std::process::id());
        file.write_all(value.as_bytes()).unwrap();
        assert!(read_token(&path).is_ok_and(|s| s == value));
        for mode in [0o640, 0o604, 0o700, 0o660] {
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
            assert!(matches!(read_token(&path), Err(Error::Outage)));
        }
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        file.set_len(0).unwrap();
        assert!(matches!(read_token(&path), Err(Error::Outage)));
        assert!(matches!(
            read_token(path.parent().unwrap()),
            Err(Error::Outage)
        ));
    }
}
