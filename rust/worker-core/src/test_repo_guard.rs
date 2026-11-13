//! Test fixtures watch checkout registrations through files, never Git on the real repo.
use std::{
    fs,
    path::{Path, PathBuf},
};

type Snapshot = Vec<(PathBuf, Option<Vec<u8>>)>;
fn snapshot(root: &Path) -> Snapshot {
    let entry = root.join(".git");
    let metadata = if entry.is_file() {
        root.join(
            fs::read_to_string(&entry)
                .unwrap()
                .trim()
                .strip_prefix("gitdir: ")
                .unwrap(),
        )
    } else {
        entry
    };
    let common = match fs::read_to_string(metadata.join("commondir")) {
        Ok(value) => metadata.join(value.trim()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => metadata,
        Err(error) => panic!("read commondir: {error}"),
    };
    let mut paths = vec![common.join("HEAD")];
    match fs::read_dir(common.join("worktrees")) {
        Ok(entries) => {
            for entry in entries {
                let path = entry.unwrap().path();
                paths.extend(["gitdir", "HEAD", "locked"].map(|name| path.join(name)));
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => panic!("read worktree registry: {error}"),
    }
    paths.sort();
    paths
        .into_iter()
        .map(|path| {
            let bytes = match fs::read(&path) {
                Ok(value) => Some(value),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => panic!("read worktree metadata: {error}"),
            };
            (path, bytes)
        })
        .collect()
}

pub(crate) struct RepoGuard {
    root: PathBuf,
    before: Snapshot,
}
impl RepoGuard {
    fn new(root: PathBuf) -> Self {
        let before = snapshot(&root);
        Self { root, before }
    }
    pub(crate) fn real() -> Self {
        Self::new(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
    }
}
impl Drop for RepoGuard {
    fn drop(&mut self) {
        if !std::thread::panicking() {
            assert_eq!(
                self.before,
                snapshot(&self.root),
                "real repository worktree registry changed"
            );
        }
    }
}

#[test]
fn registry_guard_detects_added_and_relocated_worktrees() {
    let tmp = tempfile::tempdir().unwrap();
    fs::create_dir(tmp.path().join(".git")).unwrap();
    let before = snapshot(tmp.path());
    let lane = tmp.path().join(".git/worktrees/lane");
    fs::create_dir_all(&lane).unwrap();
    fs::write(lane.join("gitdir"), "/tmp/lane/.git").unwrap();
    assert_ne!(before, snapshot(tmp.path()));
    let before = snapshot(tmp.path());
    fs::write(lane.join("gitdir"), "/tmp/moved/.git").unwrap();
    assert_ne!(before, snapshot(tmp.path()));
    fs::remove_dir_all(&lane).unwrap();
    assert_ne!(before, snapshot(tmp.path()));
}
