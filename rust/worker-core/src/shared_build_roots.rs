//! Builds the spawner keeps beside the Cargo profiles: `<target>/cli-tests/<hash>` and
//! `<target>/policy-bin/<hash>`. Preflight recognizes them as Cargo output, so the host reclaim
//! does too, and prunes them like `prune_builds`: the newest few stay, older ones go.
use crate::shared_target::cargo_output;
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::SystemTime,
};

const ROOTS: [&str; 2] = ["cli-tests", "policy-bin"];
/// Builds kept per root by the generic pruning.
const KEEP: usize = 3;

type Build = (SystemTime, PathBuf, u64);

/// Marked builds of one build root, newest first; a symlinked root is never read.
fn builds_of(root: &Path, name: &str) -> io::Result<Vec<Build>> {
    let base = root.join(name);
    if !fs::symlink_metadata(&base).is_ok_and(|meta| meta.is_dir()) {
        return Ok(vec![]);
    }
    let mut builds = vec![];
    for entry in fs::read_dir(&base)? {
        let path = entry?.path();
        if fs::symlink_metadata(&path).is_ok_and(|meta| meta.is_dir()) && cargo_output(&path) {
            let (bytes, newest) = crate::disk_hygiene::scan(&path)?;
            builds.push((newest, path, bytes));
        }
    }
    builds.sort_by(|a, b| b.cmp(a));
    Ok(builds)
}

/// True when the target holds no Git entry and at least one marked build in a build root.
pub(crate) fn recognized(target: &Path) -> bool {
    !target.join(".git").exists()
        && ROOTS
            .iter()
            .any(|name| builds_of(target, name).is_ok_and(|builds| !builds.is_empty()))
}

/// Builds beyond the newest [`KEEP`] of each root, as eviction candidates.
pub(crate) fn stale(target: &Path) -> io::Result<Vec<Build>> {
    let mut stale = vec![];
    for name in ROOTS {
        stale.extend(builds_of(target, name)?.into_iter().skip(KEEP));
    }
    Ok(stale)
}

/// Every marked build of every root, for the whole-build stage once the stale ones are gone.
pub(crate) fn all(target: &Path) -> io::Result<Vec<Build>> {
    let mut all = vec![];
    for name in ROOTS {
        all.extend(builds_of(target, name)?);
    }
    Ok(all)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn marked_builds_are_recognized_and_the_newest_stay() {
        let tmp = tempfile::tempdir().unwrap();
        for n in 0..5 {
            let build = tmp.path().join(format!("policy-bin/{n:016x}"));
            fs::create_dir_all(&build).unwrap();
            fs::write(
                build.join("CACHEDIR.TAG"),
                b"Signature: 8a477f597d28d172789f06886806bc55",
            )
            .unwrap();
        }
        fs::create_dir_all(tmp.path().join("policy-bin/unmarked")).unwrap();
        assert!(recognized(tmp.path()));
        assert_eq!(stale(tmp.path()).unwrap().len(), 2);
        assert_eq!(all(tmp.path()).unwrap().len(), 5);
        fs::create_dir(tmp.path().join(".git")).unwrap();
        assert!(!recognized(tmp.path()));
    }
}
