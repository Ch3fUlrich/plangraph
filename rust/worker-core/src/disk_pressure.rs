//! Below the free-space floor, remove idle Cargo targets largest first, each kept one with its reason.
//!
//! A target is busy only by evidence about that target: a process with its working directory,
//! an open file, its executable or `CARGO_TARGET_DIR` inside the target or its worktree, a held
//! cargo lease, a pin or an unfinished run. A build elsewhere on the host protects nothing here.
use crate::{
    disk_hygiene::{git, scan, Guards, Report},
    private_targets::{cargo_target, owned},
};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

/// A target nothing touched for this long is rebuildable (a stopped session leaves it idle).
pub const IDLE: Duration = Duration::from_secs(15 * 60);

/// A target and the directory whose live processes keep it busy.
pub struct Candidate {
    pub target: PathBuf,
    pub owner: PathBuf,
}

/// A target kept, with its size and the reason.
pub struct Kept {
    pub path: PathBuf,
    pub bytes: u64,
    pub reason: String,
}

/// `rust/target` and `target` of the given worktrees.
pub fn worktree_targets(trees: &[PathBuf]) -> Vec<Candidate> {
    trees
        .iter()
        .flat_map(|tree| {
            ["rust/target", "target"].map(|rel| Candidate {
                target: tree.join(rel),
                owner: tree.clone(),
            })
        })
        .filter(|c| c.target.is_dir())
        .collect()
}

/// Cargo targets one or two levels below `root` (a scratch directory such as `/tmp`).
pub fn scratch_targets(root: &Path) -> Vec<Candidate> {
    let dirs = |path: &Path| -> Vec<PathBuf> {
        fs::read_dir(path)
            .into_iter()
            .flatten()
            .flatten()
            .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
            .map(|e| e.path())
            .collect()
    };
    let mut found = vec![];
    for first in dirs(root) {
        if cargo_target(&first) {
            found.push(first);
        } else {
            found.extend(dirs(&first).into_iter().filter(|p| cargo_target(p)));
        }
    }
    found
        .into_iter()
        .map(|target| Candidate {
            owner: target.clone(),
            target,
        })
        .collect()
}

/// `fc-*` fast-check targets directly under `cache`.
pub fn fast_check_targets(cache: &Path) -> Vec<Candidate> {
    fs::read_dir(cache)
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with("fc-"))
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
        .map(|e| Candidate {
            owner: e.path(),
            target: e.path(),
        })
        .collect()
}

fn tracked(target: &Path, owner: &Path) -> io::Result<bool> {
    let Ok(relative) = target.strip_prefix(owner) else {
        return Ok(false);
    };
    if relative.as_os_str().is_empty() || !owner.join(".git").exists() {
        return Ok(false);
    }
    let relative = relative.to_string_lossy();
    Ok(!git(owner, &["ls-files", "--", &relative])?
        .trim()
        .is_empty())
}

/// Why `candidate` must stay, or its size when it may go.
fn keep_reason(
    candidate: &Candidate,
    guards: &Guards,
    refs: &[PathBuf],
    now: SystemTime,
) -> io::Result<Result<(u64, PathBuf), (u64, String)>> {
    let target = &candidate.target;
    let meta = fs::symlink_metadata(target)?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return Ok(Err((0, "symlink or not a directory".into())));
    }
    if !cargo_target(target) {
        return Ok(Err((0, "no Cargo markers".into())));
    }
    if !owned(target)? {
        return Ok(Err((0, "owned by another user".into())));
    }
    let canonical = target.canonicalize()?;
    let owner = candidate
        .owner
        .canonicalize()
        .unwrap_or_else(|_| candidate.owner.clone());
    let (bytes, newest) = scan(target)?;
    if let Some(process) = refs
        .iter()
        .find(|p| p.starts_with(&canonical) || p.starts_with(&owner))
    {
        return Ok(Err((
            bytes,
            format!("busy: process in {}", process.display()),
        )));
    }
    if guards.protects(&canonical) || guards.protects(&owner) {
        return Ok(Err((bytes, "pinned or unfinished run".into())));
    }
    if tracked(target, &candidate.owner)? {
        return Ok(Err((bytes, "tracked files".into())));
    }
    let idle = now.duration_since(newest).unwrap_or_default();
    if idle <= IDLE {
        return Ok(Err((
            bytes,
            format!("touched {} min ago", idle.as_secs() / 60),
        )));
    }
    Ok(Ok((bytes, canonical)))
}

/// Remove idle targets, largest first, until `free + freed` reaches `floor`.
/// Returns every kept target with its reason.
#[allow(clippy::too_many_arguments)]
pub fn reclaim(
    candidates: &[Candidate],
    guards: &Guards,
    refs: &[PathBuf],
    (free, floor): (u64, u64),
    now: SystemTime,
    apply: bool,
    report: &mut Report,
) -> Vec<Kept> {
    let mut kept = vec![];
    let mut idle = vec![];
    for candidate in candidates {
        match keep_reason(candidate, guards, refs, now) {
            Ok(Ok((bytes, canonical))) => idle.push((bytes, canonical, candidate)),
            Ok(Err((bytes, reason))) => kept.push(Kept {
                path: candidate.target.clone(),
                bytes,
                reason,
            }),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => kept.push(Kept {
                path: candidate.target.clone(),
                bytes: 0,
                reason: format!("unreadable: {error}"),
            }),
        }
    }
    idle.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    let mut gained = 0u64;
    for (bytes, canonical, candidate) in idle {
        if free.saturating_add(gained) >= floor {
            kept.push(Kept {
                path: candidate.target.clone(),
                bytes,
                reason: "free space is above the floor".into(),
            });
            continue;
        }
        // The lease is held through the removal; a build starting meanwhile keeps its target.
        let lease = match crate::cargo_cache::try_exclusive_lease(&canonical) {
            Ok(Some(lease)) => lease,
            Ok(None) => {
                kept.push(Kept {
                    path: candidate.target.clone(),
                    bytes,
                    reason: "cargo lease held".into(),
                });
                continue;
            }
            Err(error) => {
                kept.push(Kept {
                    path: candidate.target.clone(),
                    bytes,
                    reason: format!("lease unavailable: {error}"),
                });
                continue;
            }
        };
        if apply {
            if let Err(error) = fs::remove_dir_all(&canonical) {
                kept.push(Kept {
                    path: candidate.target.clone(),
                    bytes,
                    reason: format!("removal failed: {error}"),
                });
                continue;
            }
            report.freed_bytes = report.freed_bytes.saturating_add(bytes);
        }
        drop(lease);
        gained = gained.saturating_add(bytes);
        report.actions.push(format!(
            "{} {} bytes={bytes} (below free-space floor)",
            if apply { "removed" } else { "would-remove" },
            canonical.display()
        ));
    }
    kept.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
    for row in &kept {
        report.skipped.push(format!(
            "{}: kept: {} bytes={}",
            row.path.display(),
            row.reason,
            row.bytes
        ));
    }
    kept
}

/// The alert text for a run that ended below the floor with nothing freed: the top 5 kept paths.
pub fn stalled_alert(kept: &[Kept]) -> String {
    let top: Vec<String> = kept
        .iter()
        .take(5)
        .map(|k| format!("{} ({} bytes): {}", k.path.display(), k.bytes, k.reason))
        .collect();
    if top.is_empty() {
        "disk below the free-space floor, freed 0: no Cargo target candidate found".into()
    } else {
        format!(
            "disk below the free-space floor, freed 0; kept: {}",
            top.join("; ")
        )
    }
}

#[cfg(test)]
#[path = "disk_pressure_tests.rs"]
mod tests;
