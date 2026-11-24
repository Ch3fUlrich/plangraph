//! One tick at a time: a lock file holding the owner's pid. A lock whose owner is
//! gone is taken over. Without this two overlapping ticks could resume one round twice.

use std::path::{Path, PathBuf};

pub struct Lock(PathBuf);

impl Lock {
    /// None when another live process holds the lock.
    pub fn acquire(path: &Path) -> Option<Lock> {
        for _ in 0..2 {
            match std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(path)
            {
                Ok(mut f) => {
                    std::io::Write::write_all(&mut f, std::process::id().to_string().as_bytes())
                        .ok()?;
                    return Some(Lock(path.to_owned()));
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                    let owner: Option<u32> = std::fs::read_to_string(path)
                        .ok()
                        .and_then(|t| t.trim().parse().ok());
                    let alive = owner.is_some_and(|p| Path::new(&format!("/proc/{p}")).exists());
                    if alive {
                        return None;
                    }
                    std::fs::remove_file(path).ok()?;
                }
                Err(_) => return None,
            }
        }
        None
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
