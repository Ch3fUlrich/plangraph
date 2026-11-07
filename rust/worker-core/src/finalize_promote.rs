//! Publish verified objects before making the quarantine tip visible.
use crate::{finalize::VerifiedBundle, hosts::RepoEntry};

#[derive(Debug, thiserror::Error)]
pub enum PromoteError {
    #[error("promotion is unsupported on this platform")]
    Unsupported,
    #[error("invalid quarantine ref or artifact")]
    Invalid,
    #[error("object installation failed")]
    Install,
    #[error("receiver objects do not resolve")]
    Objects,
    #[error("quarantine ref publication failed")]
    Ref,
}

#[derive(Debug)]
pub struct Promoted {
    pub head: String,
    pub quarantine_ref: String,
}

#[cfg(not(unix))]
pub fn promote(
    _receiver: &RepoEntry,
    _verified: VerifiedBundle,
    _quarantine_ref: &str,
) -> Result<Promoted, PromoteError> {
    Err(PromoteError::Unsupported)
}

#[cfg(unix)]
pub fn promote(
    receiver: &RepoEntry,
    verified: VerifiedBundle,
    quarantine_ref: &str,
) -> Result<Promoted, PromoteError> {
    unix::promote(receiver, verified, quarantine_ref)
}

#[cfg(unix)]
mod unix {
    use super::*;
    use crate::{
        writer_result::{oid, safe_id},
        writer_secret::command,
    };
    use std::{fs, io, path::Path};

    fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>, PromoteError> {
        command(root, args, &[], None).map_err(|_| PromoteError::Ref)
    }

    fn identical(source: &Path, target: &Path) -> Result<(), PromoteError> {
        use std::io::Read;
        let mut a = fs::File::open(source).map_err(|_| PromoteError::Install)?;
        let mut b = fs::File::open(target).map_err(|_| PromoteError::Install)?;
        loop {
            let mut x = [0; 8192];
            let mut y = [0; 8192];
            let n = a.read(&mut x).map_err(|_| PromoteError::Install)?;
            b.read_exact(&mut y[..n])
                .map_err(|_| PromoteError::Install)?;
            if x[..n] != y[..n]
                || (n == 0 && b.read(&mut y).map_err(|_| PromoteError::Install)? != 0)
            {
                return Err(PromoteError::Install);
            }
            if n == 0 {
                return Ok(());
            }
        }
    }

    fn install(source: &Path, target: &Path) -> Result<(), PromoteError> {
        match fs::hard_link(source, target) {
            Ok(()) => fs::File::open(target)
                .and_then(|f| f.sync_all())
                .map_err(|_| PromoteError::Install),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => identical(source, target),
            Err(e) if e.raw_os_error() == Some(libc::EXDEV) => {
                // Copy privately, fsync, then publish atomically without replacing a peer.
                let parent = target.parent().ok_or(PromoteError::Invalid)?;
                let mut temp =
                    tempfile::NamedTempFile::new_in(parent).map_err(|_| PromoteError::Install)?;
                let mut input = fs::File::open(source).map_err(|_| PromoteError::Install)?;
                io::copy(&mut input, &mut temp).map_err(|_| PromoteError::Install)?;
                temp.as_file()
                    .sync_all()
                    .map_err(|_| PromoteError::Install)?;
                match fs::hard_link(temp.path(), target) {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == io::ErrorKind::AlreadyExists => identical(source, target),
                    Err(_) => Err(PromoteError::Install),
                }
            }
            Err(_) => Err(PromoteError::Install),
        }
    }

    fn resolve(root: &Path, verified: &VerifiedBundle) -> Result<(), PromoteError> {
        git(root, &["cat-file", "-e", &verified.head]).map_err(|_| PromoteError::Objects)?;
        let range = format!("{}..{}", verified.base, verified.head);
        let objects = git(
            root,
            &["rev-list", "--objects", "--no-object-names", &range],
        )
        .map_err(|_| PromoteError::Objects)?;
        for object in objects.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
            let object = std::str::from_utf8(object).map_err(|_| PromoteError::Objects)?;
            git(root, &["cat-file", "-e", object]).map_err(|_| PromoteError::Objects)?;
        }
        Ok(())
    }

    pub(super) fn promote(
        receiver: &RepoEntry,
        verified: VerifiedBundle,
        reference: &str,
    ) -> Result<Promoted, PromoteError> {
        if !reference
            .strip_prefix("refs/spawner/quarantine/")
            .is_some_and(safe_id)
            || !oid(&verified.head)
            || !oid(&verified.base)
        {
            return Err(PromoteError::Invalid);
        }
        let name = verified
            .pack
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or(PromoteError::Invalid)?;
        if !name
            .strip_prefix("pack-")
            .and_then(|s| s.strip_suffix(".pack"))
            .is_some_and(oid)
            || verified.idx != verified.pack.with_extension("idx")
        {
            return Err(PromoteError::Invalid);
        }
        let root = Path::new(&receiver.root);
        let path = git(root, &["rev-parse", "--git-path", "objects/pack"])?;
        let path = std::str::from_utf8(&path).map_err(|_| PromoteError::Invalid)?;
        let dir = root.join(path.trim());
        fs::create_dir_all(&dir).map_err(|_| PromoteError::Install)?;
        install(&verified.pack, &dir.join(name))?;
        install(&verified.idx, &dir.join(name).with_extension("idx"))?;
        fs::File::open(&dir)
            .and_then(|f| f.sync_all())
            .map_err(|_| PromoteError::Install)?;
        resolve(root, &verified)?;
        let zero = "0".repeat(verified.head.len());
        let created = git(root, &["update-ref", reference, &verified.head, &zero]).is_ok();
        if !created {
            let value = git(root, &["rev-parse", "--verify", reference])?;
            if value != format!("{}\n", verified.head).as_bytes() {
                return Err(PromoteError::Ref);
            }
        }
        if let Err(error) = resolve(root, &verified) {
            if created {
                // Never delete another writer's replacement ref.
                git(root, &["update-ref", "-d", reference, &verified.head])?;
            }
            return Err(error);
        }
        Ok(Promoted {
            head: verified.head.clone(),
            quarantine_ref: reference.into(),
        })
    }
}

#[cfg(all(test, unix))]
#[path = "finalize_promote_tests.rs"]
mod tests;
