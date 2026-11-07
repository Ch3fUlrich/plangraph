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
    #[error("{kind}")]
    Cause {
        kind: Box<PromoteError>,
        #[source]
        source: Box<dyn std::error::Error + Send + Sync>,
    },
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

    fn cause(kind: PromoteError, source: crate::writer_result::WriterError) -> PromoteError {
        PromoteError::Cause {
            kind: Box::new(kind),
            source: Box::new(source),
        }
    }

    fn git(root: &Path, args: &[&str], kind: PromoteError) -> Result<Vec<u8>, PromoteError> {
        command(root, args, &[], None).map_err(|e| cause(kind, e))
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
        install_with(source, target, |a, b| fs::hard_link(a, b))
    }

    pub(super) fn install_with(
        source: &Path,
        target: &Path,
        mut link: impl FnMut(&Path, &Path) -> io::Result<()>,
    ) -> Result<(), PromoteError> {
        use std::os::unix::fs::PermissionsExt;
        match link(source, target) {
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
                    .set_permissions(fs::Permissions::from_mode(0o444))
                    .map_err(|_| PromoteError::Install)?;
                temp.as_file()
                    .sync_all()
                    .map_err(|_| PromoteError::Install)?;
                match link(temp.path(), target) {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == io::ErrorKind::AlreadyExists => identical(source, target),
                    Err(_) => Err(PromoteError::Install),
                }
            }
            Err(_) => Err(PromoteError::Install),
        }
    }

    #[cfg(test)]
    thread_local! {
        pub(super) static BATCH_INPUTS: std::cell::RefCell<Vec<Vec<u8>>> = const { std::cell::RefCell::new(Vec::new()) };
    }

    pub(super) fn resolve(root: &Path, verified: &VerifiedBundle) -> Result<(), PromoteError> {
        let range = format!("{}..{}", verified.base, verified.head);
        let mut objects = git(
            root,
            &["rev-list", "--objects", "--no-object-names", &range],
            PromoteError::Objects,
        )?;
        objects.extend_from_slice(format!("{}\n", verified.head).as_bytes());
        #[cfg(test)]
        BATCH_INPUTS.with(|inputs| inputs.borrow_mut().push(objects.clone()));
        let output = command(root, &["cat-file", "--batch-check"], &objects, None)
            .map_err(|e| cause(PromoteError::Objects, e))?;
        let lines: Vec<_> = output
            .split(|b| *b == b'\n')
            .filter(|s| !s.is_empty())
            .collect();
        if lines.len()
            != objects
                .split(|b| *b == b'\n')
                .filter(|s| !s.is_empty())
                .count()
            || lines.iter().any(|line| {
                let fields: Vec<_> = line.split(|b| *b == b' ').collect();
                fields.len() != 3 || !fields[2].iter().all(u8::is_ascii_digit)
            })
        {
            return Err(PromoteError::Objects);
        }
        Ok(())
    }

    pub(super) fn cleanup(mut delete: impl FnMut() -> Result<(), PromoteError>) {
        // A CAS mismatch is harmless; transient failures get one retry. Never mask resolve.
        if delete().is_err() {
            let _ = delete();
        }
    }

    fn check_pack(path: &Path, hex: &str) -> Result<(), PromoteError> {
        use std::io::{Read, Seek, SeekFrom};
        let mut file = fs::File::open(path).map_err(|_| PromoteError::Install)?;
        let size = i64::try_from(hex.len() / 2).map_err(|_| PromoteError::Invalid)?;
        file.seek(SeekFrom::End(-size))
            .map_err(|_| PromoteError::Install)?;
        let mut trailer = Vec::new();
        file.read_to_end(&mut trailer)
            .map_err(|_| PromoteError::Install)?;
        let actual: String = trailer.iter().map(|b| format!("{b:02x}")).collect();
        if actual != hex {
            return Err(PromoteError::Invalid);
        }
        Ok(())
    }

    pub(super) fn promote(
        receiver: &RepoEntry,
        verified: VerifiedBundle,
        reference: &str,
    ) -> Result<Promoted, PromoteError> {
        promote_with(receiver, verified, reference, resolve)
    }

    pub(super) fn promote_with(
        receiver: &RepoEntry,
        verified: VerifiedBundle,
        reference: &str,
        mut resolve: impl FnMut(&Path, &VerifiedBundle) -> Result<(), PromoteError>,
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
        check_pack(&verified.pack, &name[5..name.len() - 5])?;
        let path = git(
            root,
            &["rev-parse", "--git-path", "objects/pack"],
            PromoteError::Install,
        )?;
        let path = std::str::from_utf8(&path).map_err(|_| PromoteError::Install)?;
        let dir = root.join(path.trim());
        fs::create_dir_all(&dir).map_err(|_| PromoteError::Install)?;
        install(&verified.pack, &dir.join(name))?;
        install(&verified.idx, &dir.join(name).with_extension("idx"))?;
        fs::File::open(&dir)
            .and_then(|f| f.sync_all())
            .map_err(|_| PromoteError::Install)?;
        resolve(root, &verified)?;
        let zero = "0".repeat(verified.head.len());
        let created = git(
            root,
            &["update-ref", reference, &verified.head, &zero],
            PromoteError::Ref,
        )
        .is_ok();
        if !created {
            let value = git(
                root,
                &["rev-parse", "--verify", reference],
                PromoteError::Ref,
            )?;
            if value != format!("{}\n", verified.head).as_bytes() {
                return Err(PromoteError::Ref);
            }
        }
        if let Err(error) = resolve(root, &verified) {
            if created {
                // Never delete another writer's replacement ref.
                cleanup(|| {
                    git(
                        root,
                        &["update-ref", "-d", reference, &verified.head],
                        PromoteError::Ref,
                    )
                    .map(|_| ())
                });
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
