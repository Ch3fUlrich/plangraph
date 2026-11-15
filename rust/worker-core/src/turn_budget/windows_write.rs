//! Windows publication pipeline; real file IO and fault injection share this path.
use super::rename::{self, Step};
use std::{fs, io, io::Write, path::Path};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Stage {
    Absolute,
    CanonicalParent,
    CreateDelete,
    CreatePlain,
    Write,
    WriteOpen,
    WritePath,
    Metadata,
    MetadataOpen,
    MetadataHandle,
    Permissions,
    PermissionsPath,
    Sync,
    SyncOpen,
    SyncReopened,
    Posix,
    Replace,
    Legacy,
    Move,
}

pub(super) struct Calls<F, R> {
    pub(super) before: F,
    pub(super) report: R,
}

impl<F, R> Calls<F, R>
where
    F: FnMut(Stage) -> io::Result<()>,
    R: FnMut(Stage, &io::Error),
{
    pub(super) fn run<T>(
        &mut self,
        stage: Stage,
        call: impl FnOnce() -> io::Result<T>,
    ) -> io::Result<T> {
        let result = (self.before)(stage).and_then(|()| call());
        if let Err(error) = &result {
            if !(matches!(stage, Stage::Metadata | Stage::MetadataOpen)
                && error.kind() == io::ErrorKind::NotFound)
            {
                (self.report)(stage, error);
            }
        }
        result
    }

    fn fallback<T>(
        &mut self,
        stage: Stage,
        call: impl FnOnce() -> io::Result<T>,
        fallback: impl FnOnce(&mut Self) -> io::Result<T>,
    ) -> io::Result<T> {
        match self.run(stage, call) {
            Err(e) if rename::refused(&e) => fallback(self),
            result => result,
        }
    }
}

pub(super) fn atomic_write<F, R>(
    path: &Path,
    bytes: &[u8],
    calls: &mut Calls<F, R>,
) -> io::Result<()>
where
    F: FnMut(Stage) -> io::Result<()>,
    R: FnMut(Stage, &io::Error),
{
    let target = calls.fallback(
        Stage::Absolute,
        || std::path::absolute(path),
        |calls| {
            calls.run(Stage::CanonicalParent, || {
                let parent = fs::canonicalize(path.parent().unwrap_or(Path::new(".")))?;
                Ok(parent.join(path.file_name().ok_or(io::ErrorKind::InvalidInput)?))
            })
        },
    )?;
    let parent = target.parent().ok_or(io::ErrorKind::InvalidInput)?;
    let (mut file, by_handle) = calls.fallback(
        Stage::CreateDelete,
        || create(parent, true).map(|file| (file, true)),
        |calls| {
            calls.run(Stage::CreatePlain, || {
                create(parent, false).map(|file| (file, false))
            })
        },
    )?;
    let temporary_path = file.path().to_owned();
    calls.fallback(
        Stage::Write,
        || file.write_all(bytes),
        |calls| {
            let mut reopened = calls.run(Stage::WriteOpen, || {
                fs::OpenOptions::new().write(true).open(&temporary_path)
            })?;
            calls.run(Stage::WritePath, || reopened.write_all(bytes))
        },
    )?;
    let metadata = calls.fallback(
        Stage::Metadata,
        || fs::metadata(&target),
        |calls| {
            let existing = calls.run(Stage::MetadataOpen, || fs::File::open(&target))?;
            calls.run(Stage::MetadataHandle, || existing.metadata())
        },
    );
    match metadata {
        Ok(metadata) => calls.fallback(
            Stage::Permissions,
            || file.as_file().set_permissions(metadata.permissions()),
            |calls| {
                calls.run(Stage::PermissionsPath, || {
                    fs::set_permissions(file.path(), metadata.permissions())
                })
            },
        )?,
        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    calls.fallback(
        Stage::Sync,
        || file.as_file().sync_all(),
        |calls| {
            let reopened = calls.run(Stage::SyncOpen, || {
                fs::OpenOptions::new().write(true).open(file.path())
            })?;
            calls.run(Stage::SyncReopened, || reopened.sync_all())
        },
    )?;
    replace_status(&mut file, &target, by_handle, calls)
}

fn create(parent: &Path, delete: bool) -> io::Result<tempfile::NamedTempFile> {
    tempfile::Builder::new().make_in(parent, |name| {
        let mut options = fs::OpenOptions::new();
        // access_mode does NOT satisfy Rust's create_new validation by itself.
        options.read(true).write(true).create_new(true);
        #[cfg(windows)]
        if delete {
            use std::os::windows::fs::OpenOptionsExt;
            use windows_sys::Win32::Storage::FileSystem::{
                DELETE, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
            };
            options.access_mode(DELETE | FILE_GENERIC_READ | FILE_GENERIC_WRITE);
        }
        #[cfg(not(windows))]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
            let _ = delete;
        }
        options.open(name)
    })
}

fn replace_status<F, R>(
    file: &mut tempfile::NamedTempFile,
    path: &Path,
    by_handle: bool,
    calls: &mut Calls<F, R>,
) -> io::Result<()>
where
    F: FnMut(Stage) -> io::Result<()>,
    R: FnMut(Stage, &io::Error),
{
    let mut call = |step| {
        let stage = match step {
            Step::Posix => Stage::Posix,
            Step::Replace => Stage::Replace,
            Step::Legacy => Stage::Legacy,
            Step::Move => Stage::Move,
        };
        calls.run(stage, || {
            #[cfg(windows)]
            {
                super::windows_rename::replace(file, path, step)
            }
            #[cfg(not(windows))]
            {
                fs::rename(file.path(), path)
            }
        })
    };
    if by_handle {
        rename::rename_with_fallback(&mut call)?;
    } else {
        call(Step::Move)?;
    }
    // Never remove a future file reusing the old temporary name.
    file.disable_cleanup(true);
    Ok(())
}

#[cfg(test)]
#[path = "windows_write_tests.rs"]
mod tests;
