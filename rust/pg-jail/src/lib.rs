//! Landlock filesystem jail helper.
//!
//! Reading is limited to the `ro` and `rw` lists; callers must list system
//! directories (`/usr`, `/lib`, `/lib64`, `/bin`, the needed `/etc` subset,
//! `/proc`, `/dev`) in `ro`. Everything not listed is denied.
//!
//! Missing `rw`/`ro` paths and `deny_read` paths that overlap an allow entry
//! are rejected before the ruleset is applied (fail closed).

use std::ffi::{OsStr, OsString};
use std::io;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use landlock::{
    Access, AccessFs, CompatLevel, Compatible, PathBeneath, PathFd, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, ABI,
};

/// Allow-list and deny-list for a Landlock ruleset.
#[derive(Debug, Clone, Default)]
pub struct JailSpec {
    /// Paths with full read/write/execute access.
    pub rw: Vec<PathBuf>,
    /// Paths with read/execute access.
    pub ro: Vec<PathBuf>,
    /// Paths that must not get a rule. They must not overlap an allow path.
    pub deny_read: Vec<PathBuf>,
    /// Keep going when the kernel only partially enforces the ruleset.
    pub best_effort: bool,
}

/// Errors returned while building or applying a jail.
#[derive(Debug, thiserror::Error)]
pub enum JailError {
    /// Landlock is unavailable or the ruleset was not fully enforced.
    #[error("landlock unsupported")]
    Unsupported,
    /// A path could not be validated or opened.
    #[error("path {0}: {1}")]
    Path(PathBuf, io::Error),
    /// The ruleset could not be built or applied.
    #[error("landlock ruleset: {0}")]
    Ruleset(String),
}

/// Apply the jail to the current process.
pub fn apply(spec: &JailSpec) -> Result<(), JailError> {
    validate(spec)?;

    let abi = ABI::V1;
    let all = AccessFs::from_all(abi);
    let read_exec = AccessFs::ReadFile | AccessFs::ReadDir | AccessFs::Execute;

    let mut ruleset = Ruleset::default()
        .set_compatibility(CompatLevel::BestEffort)
        .handle_access(all)
        .map_err(ruleset_err)?
        .create()
        .map_err(ruleset_err)?;

    for path in &spec.rw {
        let fd = PathFd::new(path).map_err(|e| path_err(path, e))?;
        // A file takes file rights only: directory rights on it would make the kernel
        // report a partial ruleset.
        let access = if std::fs::metadata(path)
            .map_err(|e| path_err(path, e))?
            .is_dir()
        {
            all
        } else {
            AccessFs::from_file(abi)
        };
        ruleset = ruleset
            .add_rule(PathBeneath::new(fd, access))
            .map_err(ruleset_err)?;
    }

    for path in &spec.ro {
        let fd = PathFd::new(path).map_err(|e| path_err(path, e))?;
        let access = if std::fs::metadata(path)
            .map_err(|e| path_err(path, e))?
            .is_dir()
        {
            read_exec
        } else {
            AccessFs::from_read(abi) & AccessFs::from_file(abi)
        };
        ruleset = ruleset
            .add_rule(PathBeneath::new(fd, access))
            .map_err(ruleset_err)?;
    }

    let status = ruleset.restrict_self().map_err(ruleset_err)?;
    match status.ruleset {
        RulesetStatus::FullyEnforced => Ok(()),
        _ if spec.best_effort => Ok(()),
        _ => Err(JailError::Unsupported),
    }
}

/// Apply the jail and exec `program`.
///
/// Returns an error if the jail cannot be applied or if exec fails; on success
/// this function does not return.
pub fn jail_exec(spec: &JailSpec, program: &OsStr, args: &[OsString]) -> JailError {
    if let Err(e) = apply(spec) {
        return e;
    }

    let err = Command::new(program).args(args).exec();
    JailError::Path(PathBuf::from(program), err)
}

fn validate(spec: &JailSpec) -> Result<(), JailError> {
    for path in spec.rw.iter().chain(spec.ro.iter()) {
        std::fs::metadata(path).map_err(|e| JailError::Path(path.clone(), e))?;
    }

    for deny in &spec.deny_read {
        for allow in spec.rw.iter().chain(spec.ro.iter()) {
            if deny == allow || deny.starts_with(allow) || allow.starts_with(deny) {
                return Err(JailError::Path(
                    deny.clone(),
                    io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "deny_read overlaps an allowed path",
                    ),
                ));
            }
        }
    }

    Ok(())
}

fn path_err<E: std::fmt::Display>(path: &Path, e: E) -> JailError {
    JailError::Path(path.to_path_buf(), io::Error::other(e.to_string()))
}

fn ruleset_err<E: std::fmt::Display>(e: E) -> JailError {
    JailError::Ruleset(e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn deny_read_under_ro_is_rejected() {
        let dir = tempdir().unwrap();
        let ro = dir.path().to_path_buf();
        let deny = ro.join("secret");
        let spec = JailSpec {
            rw: Vec::new(),
            ro: vec![ro],
            deny_read: vec![deny.clone()],
            best_effort: false,
        };

        match validate(&spec) {
            Err(JailError::Path(path, _)) => assert_eq!(path, deny),
            other => panic!("expected path error, got {other:?}"),
        }
    }

    #[test]
    fn missing_allow_path_is_rejected() {
        let dir = tempdir().unwrap();
        let missing = dir.path().join("missing");
        let spec = JailSpec {
            rw: vec![missing.clone()],
            ro: Vec::new(),
            deny_read: Vec::new(),
            best_effort: false,
        };

        match validate(&spec) {
            Err(JailError::Path(path, _)) => assert_eq!(path, missing),
            other => panic!("expected path error, got {other:?}"),
        }
    }
}
