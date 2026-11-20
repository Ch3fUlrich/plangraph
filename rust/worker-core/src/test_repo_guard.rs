//! Test fixtures watch checkout registrations through files, never Git on the real repo.
use std::{
    fs,
    path::{Path, PathBuf},
};

type Snapshot = Vec<(PathBuf, Option<Vec<u8>>)>;
const GIT_ENTRY: &str = ".git";
fn snapshot(root: &Path, registry_root: Option<&Path>) -> Snapshot {
    let entry = root.join(GIT_ENTRY);
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
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => metadata.clone(),
        Err(error) => panic!("read commondir: {error}"),
    };
    let mut paths = vec![common.join("config"), metadata.join("config.worktree")];
    if registry_root.is_none() {
        paths.push(common.join("HEAD"));
    }
    match fs::read_dir(common.join("worktrees")) {
        Ok(entries) => {
            for entry in entries {
                let path = entry.unwrap().path();
                if let Some(owned) = registry_root {
                    let target = match fs::read_to_string(path.join("gitdir")) {
                        Ok(target) => crate::hosts::canonicalize_prefix(target.trim())
                            .unwrap_or_else(|| PathBuf::from(target.trim())),
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                        Err(error) => panic!("read worktree gitdir: {error}"),
                    };
                    if !target.starts_with(owned) {
                        continue;
                    }
                }
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
    registry_root: Option<PathBuf>,
}
impl RepoGuard {
    fn new(root: PathBuf, registry_root: Option<PathBuf>) -> Self {
        let before = snapshot(&root, registry_root.as_deref());
        Self {
            root,
            before,
            registry_root,
        }
    }
    pub(crate) fn real(fixture_root: &Path) -> Self {
        Self::new(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
            Some(fixture_root.canonicalize().unwrap()),
        )
    }
}
impl Drop for RepoGuard {
    fn drop(&mut self) {
        if !std::thread::panicking() {
            assert_eq!(
                self.before,
                snapshot(&self.root, self.registry_root.as_deref()),
                "real repository config or fixture worktree registry changed"
            );
        }
    }
}

#[test]
fn registry_guard_detects_added_and_relocated_worktrees() {
    let tmp = tempfile::tempdir().unwrap();
    fs::create_dir(tmp.path().join(GIT_ENTRY)).unwrap();
    let before = snapshot(tmp.path(), None);
    let lane = tmp.path().join(".git/worktrees/lane");
    fs::create_dir_all(&lane).unwrap();
    fs::write(lane.join("gitdir"), "/tmp/lane/.git").unwrap();
    assert_ne!(before, snapshot(tmp.path(), None));
    let before = snapshot(tmp.path(), None);
    fs::write(lane.join("gitdir"), "/tmp/moved/.git").unwrap();
    assert_ne!(before, snapshot(tmp.path(), None));
    fs::remove_dir_all(&lane).unwrap();
    assert_ne!(before, snapshot(tmp.path(), None));
}

#[test]
fn scoped_guard_ignores_other_sessions_but_detects_owned_changes_and_config() {
    let tmp = tempfile::tempdir().unwrap();
    let fixture = tmp.path().join("fixture");
    fs::create_dir(&fixture).unwrap();
    let owned = fixture.canonicalize().unwrap();
    let metadata = tmp.path().join(GIT_ENTRY);
    let lane = metadata.join("worktrees/lane");
    fs::create_dir_all(&lane).unwrap();
    fs::write(
        lane.join("gitdir"),
        tmp.path().join("other/.git").to_str().unwrap(),
    )
    .unwrap();
    let before = snapshot(tmp.path(), Some(&owned));
    fs::write(
        lane.join("gitdir"),
        tmp.path().join("moved/.git").to_str().unwrap(),
    )
    .unwrap();
    assert_eq!(before, snapshot(tmp.path(), Some(&owned)));
    fs::write(
        lane.join("gitdir"),
        fixture.join(GIT_ENTRY).to_str().unwrap(),
    )
    .unwrap();
    assert_ne!(before, snapshot(tmp.path(), Some(&owned)));
    let before = snapshot(tmp.path(), Some(&owned));
    fs::write(lane.join("HEAD"), "changed").unwrap();
    assert_ne!(before, snapshot(tmp.path(), Some(&owned)));
    let before = snapshot(tmp.path(), Some(&owned));
    fs::write(metadata.join("config"), "changed").unwrap();
    assert_ne!(before, snapshot(tmp.path(), Some(&owned)));
    let before = snapshot(tmp.path(), Some(&owned));
    fs::write(metadata.join("config.worktree"), "changed").unwrap();
    assert_ne!(before, snapshot(tmp.path(), Some(&owned)));
}
