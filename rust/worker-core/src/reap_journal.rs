//! Exact approval rows and durable receipts for staged deletion.
use crate::{disk_hygiene, run::record_io};
use pg_reap::Worktree;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeSet,
    fs, io,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub(crate) const RETENTION: Duration = Duration::from_secs(7 * 86_400);

#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Row {
    pub action: String,
    pub kind: String,
    pub repo: PathBuf,
    pub path: PathBuf,
    pub original: PathBuf,
    pub branch: Option<String>,
    pub head: String,
    pub device: u64,
    pub inode: u64,
    pub bytes: u64,
    pub reason: String,
    pub record_hash: Option<String>,
}

impl Row {
    pub fn new(repo: &Path, path: &Path, original: &Path, tree: &Worktree) -> io::Result<Self> {
        let mut row = Self::for_path(repo, path, original, "worktree")?;
        row.branch = tree.branch.clone();
        row.head = tree.head.clone();
        Ok(row)
    }

    pub fn for_path(repo: &Path, path: &Path, original: &Path, kind: &str) -> io::Result<Self> {
        let meta = directory(path)?;
        let bytes = disk_hygiene::scan(path)?.0;
        Ok(Self {
            action: if path == original {
                "would-quarantine"
            } else {
                "would-delete"
            }
            .into(),
            kind: kind.into(),
            repo: repo.into(),
            path: path.into(),
            original: original.into(),
            branch: None,
            head: String::new(),
            device: meta.dev(),
            inode: meta.ino(),
            bytes,
            reason: if kind == "worktree" {
                "merged-clean-older-than-7d"
            } else {
                "terminal-landed-no-checkout-older-than-7d"
            }
            .into(),
            record_hash: None,
        })
    }

    pub fn same_directory(&self, path: &Path) -> bool {
        directory(path).is_ok_and(|meta| meta.dev() == self.device && meta.ino() == self.inode)
    }

    pub fn same_worktree(&self, tree: &Worktree) -> bool {
        self.head == tree.head
            && self.branch == tree.branch
            && self.same_directory(Path::new(&tree.path))
    }
}

pub(crate) fn directory(path: &Path) -> io::Result<fs::Metadata> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || path.canonicalize()? != path {
        return Err(io::Error::other("not a canonical directory"));
    }
    Ok(meta)
}

pub(crate) struct Approval<'a> {
    pub id: &'a str,
    pub rows: BTreeSet<Row>,
}

impl<'a> Approval<'a> {
    pub fn parse(id: &'a str, plan: &str) -> io::Result<Self> {
        let valid = id.strip_prefix("D-").is_some_and(|n| {
            !n.is_empty() && !n.starts_with('0') && n.bytes().all(|b| b.is_ascii_digit())
        });
        if !valid {
            return Err(io::Error::other("approval must be a routing D-id"));
        }
        let rows = plan
            .lines()
            .map(serde_json::from_str)
            .collect::<Result<_, _>>()?;
        Ok(Self { id, rows })
    }
}

#[derive(Deserialize, Serialize)]
pub(crate) struct Receipt {
    pub row: Row,
    pub approval: String,
    pub quarantined_at: Option<u64>,
}

impl Receipt {
    pub fn read(slot: &Path) -> io::Result<Self> {
        Ok(serde_json::from_slice(&fs::read(
            slot.join("receipt.json"),
        )?)?)
    }

    pub fn write(&self, slot: &Path) -> io::Result<()> {
        record_io::atomic_write_private(&slot.join("receipt.json"), &serde_json::to_vec(self)?)?;
        fs::File::open(slot)?.sync_all()
    }

    pub fn ready(&self, now: u64) -> bool {
        self.quarantined_at
            .and_then(|start| now.checked_sub(start))
            .is_some_and(|age| age >= RETENTION.as_secs())
    }
}

pub(crate) fn timestamp() -> io::Result<u64> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs())
}

pub(crate) fn audit(state: &Path, verb: &str, approval: &str, row: &Row) -> io::Result<()> {
    let mut line = serde_json::to_vec(&serde_json::json!({
        "event": verb, "ts": timestamp()?, "approval": approval, "entry": row,
    }))?;
    line.push(b'\n');
    record_io::append_private(&state.join("reap.ndjson"), &line)
}

pub(crate) fn private_directory(path: &Path) -> io::Result<()> {
    let meta = directory(path)?;
    // SAFETY: geteuid has no preconditions.
    if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err(io::Error::other("quarantine directory is not private"));
    }
    Ok(())
}

pub(crate) fn stage(
    state: &Path,
    quarantine: &Path,
    approval: &str,
    row: &Row,
    move_to: impl FnOnce(&Path) -> io::Result<()>,
) -> io::Result<String> {
    record_io::ensure_private_dir(quarantine)?;
    private_directory(quarantine)?;
    let slot = tempfile::Builder::new()
        .prefix("entry-")
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir_in(quarantine)?
        .keep();
    let mut receipt = Receipt {
        row: row.clone(),
        approval: approval.into(),
        quarantined_at: None,
    };
    receipt.write(&slot)?;
    fs::File::open(quarantine)?.sync_all()?;
    audit(state, "quarantine-intent", approval, row)?;
    let dest = slot.join(&row.kind);
    if let Err(error) = move_to(&dest) {
        // A Git move can fail after renaming the checkout but before updating its metadata.
        if matches!(dest.symlink_metadata(), Err(e) if e.kind() == io::ErrorKind::NotFound) {
            fs::remove_file(slot.join("receipt.json"))
                .and_then(|()| fs::remove_dir(&slot))
                .map_err(|cleanup| {
                    io::Error::other(format!("{error}, quarantine cleanup failed: {cleanup}"))
                })?;
        }
        return Err(error);
    }
    receipt.quarantined_at = Some(timestamp()?);
    receipt.write(&slot)?;
    audit(state, "quarantined", approval, row)?;
    Ok(format!(
        "quarantined {}",
        row.original.to_string_lossy().escape_debug()
    ))
}

pub(crate) fn delete(
    state: &Path,
    slot: &Path,
    approval: &str,
    row: &Row,
    remove: impl FnOnce() -> io::Result<()>,
) -> io::Result<String> {
    audit(state, "delete-intent", approval, row)?;
    remove()?;
    audit(state, "deleted", approval, row)?;
    fs::remove_file(slot.join("receipt.json"))?;
    fs::remove_dir(slot)?;
    Ok(format!(
        "deleted {} {} bytes",
        row.original.to_string_lossy().escape_debug(),
        row.bytes
    ))
}

/// A bind mount may share a device number, so metadata alone cannot exclude it.
pub(crate) fn check_mounts(path: &Path) -> io::Result<()> {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};

    for line in fs::read("/proc/self/mountinfo")?
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
    {
        let mount = line
            .split(|b| *b == b' ')
            .nth(4)
            .ok_or_else(|| io::Error::other("invalid mount inventory"))?;
        let mut bytes = Vec::with_capacity(mount.len());
        let mut remaining = mount;
        while let Some((&byte, tail)) = remaining.split_first() {
            if byte == b'\\' {
                let escaped = tail
                    .get(..3)
                    .and_then(|s| std::str::from_utf8(s).ok())
                    .and_then(|s| u8::from_str_radix(s, 8).ok())
                    .ok_or_else(|| io::Error::other("invalid mount escape"))?;
                bytes.push(escaped);
                remaining = &tail[3..];
            } else {
                bytes.push(byte);
                remaining = tail;
            }
        }
        if PathBuf::from(OsString::from_vec(bytes)).starts_with(path) {
            return Err(io::Error::other("mount inside candidate"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_quarantine_move_removes_only_an_empty_slot() {
        for moved in [false, true] {
            let temp = tempfile::tempdir().unwrap();
            let state = temp.path().canonicalize().unwrap();
            let source = state.join("source");
            fs::create_dir(&source).unwrap();
            let quarantine = state.join("quarantine");
            let row = Row::for_path(&state, &source, &source, "worktree").unwrap();
            let refusal = stage(&state, &quarantine, "D-123", &row, |dest| {
                if moved {
                    fs::rename(&source, dest)?;
                }
                Err(io::Error::other("fixture move failure"))
            })
            .unwrap_err();
            assert_eq!(refusal.to_string(), "fixture move failure");
            let slots = fs::read_dir(&quarantine)
                .unwrap()
                .collect::<io::Result<Vec<_>>>()
                .unwrap();
            assert_eq!(slots.len(), usize::from(moved));
            if let Some(slot) = slots.first() {
                assert!(slot.path().join("worktree").is_dir());
                assert!(Receipt::read(&slot.path())
                    .unwrap()
                    .quarantined_at
                    .is_none());
            }
        }
    }
}
