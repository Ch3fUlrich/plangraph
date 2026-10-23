//! Landlock filesystem jail helper.
//!
//! Reading is limited to the `ro` and `rw` lists; callers must list system
//! directories (`/usr`, `/lib`, `/lib64`, `/bin`, the needed `/etc` subset,
//! `/dev`) in `ro`. Use `proc_self` for restricted procfs access.
//! Everything not listed is denied.
//!
//! The ruleset is built for Landlock [`MIN_ABI`] (V3): that is the ABI that
//! added [`AccessFs::Truncate`], so only from V3 on can the kernel deny
//! `truncate(2)` of a file outside the allowed roots. Kernels that cannot
//! enforce that ABI are refused with [`JailError::Unsupported`] (exit status
//! 78 in the CLI); there is no best-effort fallback.
//!
//! Missing `rw`/`ro` paths and `deny_read` paths that overlap an allow entry
//! are rejected before the ruleset is applied (fail closed).

use std::io;
use std::path::{Path, PathBuf};

use landlock::{
    Access, AccessFs, CompatLevel, Compatible, PathBeneath, PathFd, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, ABI,
};

/// Lowest Landlock ABI the jail can be enforced at.
///
/// ABI V3 introduced [`AccessFs::Truncate`]. With an older ABI the kernel
/// never checks that right, so `truncate(2)` of a file outside every allowed
/// root would succeed inside the jail.
pub const MIN_ABI: ABI = ABI::V3;

/// Allow-list and deny-list for a Landlock ruleset.
#[derive(Debug, Clone, Default)]
pub struct JailSpec {
    /// Paths with full read/write/execute access.
    pub rw: Vec<PathBuf>,
    /// Paths with read/execute access.
    pub ro: Vec<PathBuf>,
    /// Paths that must not get a rule. Landlock is an allow-list, so a path
    /// here is only readable if some allow entry already covers it; such an
    /// overlap is rejected by [`apply`] instead of being silently ignored.
    pub deny_read: Vec<PathBuf>,
    /// Allow this process's procfs directory and selected system information files.
    pub proc_self: bool,
}

/// Errors returned while building or applying a jail.
#[derive(Debug, thiserror::Error)]
pub enum JailError {
    /// Landlock is unavailable, the kernel enforces an ABI older than
    /// [`MIN_ABI`] (so `truncate(2)` outside the allowed roots could not be
    /// denied), or the ruleset was not fully enforced.
    #[error("landlock unsupported")]
    Unsupported,
    /// A path could not be validated or opened.
    #[error("path {0}: {1}")]
    Path(PathBuf, io::Error),
    /// The ruleset could not be built or applied.
    #[error("landlock ruleset: {0}")]
    Ruleset(String),
}

/// Selects the ABI the ruleset is built for.
///
/// `kernel_abi` is the ABI reported by the running kernel. Anything below
/// [`MIN_ABI`] cannot deny `truncate(2)` outside the allowed roots, so it is
/// refused with [`JailError::Unsupported`] (exit status 78 in the CLI). A
/// kernel at or above [`MIN_ABI`] is pinned to [`MIN_ABI`], so the enforced
/// policy does not change with the kernel version.
pub fn select_abi(kernel_abi: ABI) -> Result<ABI, JailError> {
    if kernel_abi >= MIN_ABI {
        Ok(MIN_ABI)
    } else {
        Err(JailError::Unsupported)
    }
}

/// Apply the jail to the current process.
pub fn apply(spec: &JailSpec) -> Result<(), JailError> {
    let mut spec = spec.clone();
    if spec.proc_self {
        spec.ro.retain(|path| path != Path::new("/proc"));
        // The PID survives exec; granting /proc itself would expose sibling secrets.
        spec.ro
            .push(PathBuf::from(format!("/proc/{}", std::process::id())));
        for path in [
            "/proc/cpuinfo",
            "/proc/meminfo",
            "/proc/stat",
            "/proc/loadavg",
        ] {
            if Path::new(path)
                .try_exists()
                .map_err(|e| path_err(Path::new(path), e))?
            {
                spec.ro.push(PathBuf::from(path));
            }
        }
    }
    validate(&spec)?;

    // Pin the ruleset to MIN_ABI. CompatLevel::HardRequirement makes
    // handle_access() fail - and apply() return JailError::Unsupported - when
    // the running kernel cannot enforce V3, i.e. when it could not deny
    // truncate(2) outside the allowed roots.
    let abi = MIN_ABI;
    // from_all(MIN_ABI) carries every right up to V3, including Refer (V2,
    // rename/link across roots) and Truncate (V3).
    let all = AccessFs::from_all(abi);
    let read_exec = AccessFs::ReadFile | AccessFs::ReadDir | AccessFs::Execute;

    let mut ruleset = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(all)
        .map_err(|_| JailError::Unsupported)?
        .create()
        .map_err(|_| JailError::Unsupported)?;

    for path in &spec.rw {
        let fd = PathFd::new(path).map_err(|e| path_err(path, e))?;
        // A file takes file rights only: directory rights on it would make the kernel
        // report a partial ruleset. Truncate is part of those file rights, so an
        // rw root (and an rw file) may be truncated.
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
        // Read-only roots never get Truncate (or any write right), so
        // truncate(2) on them is denied like everywhere else outside rw roots.
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
    if status.ruleset != RulesetStatus::FullyEnforced {
        return Err(JailError::Unsupported);
    }
    // Second, explicit gate on the ABI the kernel reported for this ruleset:
    // below MIN_ABI the confinement would be weaker than documented, so refuse
    // instead of running.
    select_abi(ABI::from(status.landlock))?;
    Ok(())
}

fn validate(spec: &JailSpec) -> Result<(), JailError> {
    if spec.ro.iter().any(|path| path == Path::new("/proc")) {
        return Err(JailError::Path(
            PathBuf::from("/proc"),
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "use proc_self instead of allowing /proc",
            ),
        ));
    }
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
                        "deny-read path lies under an allowed root: Landlock is an \
                         allow-list and cannot revoke access inside an allowed root",
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
            proc_self: false,
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
            proc_self: false,
        };

        match validate(&spec) {
            Err(JailError::Path(path, _)) => assert_eq!(path, missing),
            other => panic!("expected path error, got {other:?}"),
        }
    }

    #[test]
    fn select_abi_refuses_below_min_abi() {
        // The kernel cannot be faked here, so the gate itself is unit-tested:
        // every ABI that lacks AccessFs::Truncate must be refused.
        for kernel_abi in [ABI::Unsupported, ABI::V1, ABI::V2] {
            match select_abi(kernel_abi) {
                Err(JailError::Unsupported) => {}
                other => panic!("ABI {kernel_abi:?} must be refused, got {other:?}"),
            }
        }

        for kernel_abi in [MIN_ABI, ABI::V4, ABI::V9] {
            assert_eq!(select_abi(kernel_abi).unwrap(), MIN_ABI);
        }
    }

    #[test]
    fn min_abi_carries_truncate_and_refer() {
        let all = AccessFs::from_all(MIN_ABI);
        assert!(all.contains(AccessFs::Truncate));
        assert!(all.contains(AccessFs::Refer));
        // Read-only roots never grant truncation.
        let read = AccessFs::from_read(MIN_ABI) & AccessFs::from_file(MIN_ABI);
        assert!(!read.contains(AccessFs::Truncate));
    }

    #[test]
    fn deny_read_message_is_explicit() {
        let dir = tempdir().unwrap();
        let rw = dir.path().to_path_buf();
        let deny = rw.join("secret");
        let spec = JailSpec {
            rw: vec![rw],
            ro: Vec::new(),
            deny_read: vec![deny],
            proc_self: false,
        };

        match validate(&spec) {
            Err(JailError::Path(_, err)) => {
                let msg = err.to_string();
                assert!(msg.contains("deny-read"), "{msg}");
                assert!(msg.contains("allowed root"), "{msg}");
            }
            other => panic!("expected path error, got {other:?}"),
        }
    }

    // `LandlockStatus` is only reachable through a completed restriction; the
    // helper is the single place that decides whether an ABI is acceptable.
    #[test]
    fn select_abi_rejects_unavailable_landlock() {
        use landlock::LandlockStatus;
        let status = LandlockStatus::NotEnabled;
        match select_abi(ABI::from(status)) {
            Err(JailError::Unsupported) => {}
            other => panic!("unavailable Landlock must be refused, got {other:?}"),
        }
    }
}
