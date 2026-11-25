#![cfg(unix)]
//! Worktrees named by a user unit survive the sweep; units naming missing worktrees are listed.
use pg_worker_core::{
    disk_hygiene::Guards,
    merged_sweep::{run_with_authority, Options},
    sweep_facts::{Claims, Protection, Pull, UnitRef},
};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};
fn git(repo: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}
struct Fixture {
    _tmp: tempfile::TempDir,
    repo: PathBuf,
    root: PathBuf,
    state: PathBuf,
    sha: String,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("repo");
        let root = tmp.path().join("trees");
        let state = tmp.path().join("state");
        for path in [&repo, &root, &state] {
            fs::create_dir(path).unwrap();
        }
        git(&repo, &["init", "-qb", "main"]);
        fs::write(repo.join("one"), "one").unwrap();
        git(&repo, &["add", "."]);
        git(&repo, &["commit", "-qm", "initial"]);
        let remote = tmp.path().join("remote");
        fs::create_dir(&remote).unwrap();
        git(&remote, &["init", "--bare", "-q"]);
        git(
            &repo,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "main"]);
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-qb",
                "topic",
                root.join("topic").to_str().unwrap(),
            ],
        );
        git(&repo, &["push", "-q", "origin", "topic"]);
        let tree_git = git(&root.join("topic"), &["rev-parse", "--absolute-git-dir"]);
        for name in ["HEAD", "index"] {
            fs::File::options()
                .write(true)
                .open(Path::new(tree_git.trim()).join(name))
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(SystemTime::now() - Duration::from_secs(90000)),
                )
                .unwrap();
        }
        fs::write(state.join("pins.json"), "[]").unwrap();
        let sha = git(&repo, &["rev-parse", "HEAD"]).trim().to_string();
        Self {
            _tmp: tmp,
            repo,
            root,
            state,
            sha,
        }
    }
}
#[test]
fn sweep_refuses_a_worktree_a_user_unit_names_and_removes_an_unreferenced_one() {
    let f = Fixture::new();
    git(
        &f.repo,
        &[
            "worktree",
            "add",
            "-qb",
            "spare",
            f.root.join("spare").to_str().unwrap(),
        ],
    );
    git(&f.repo, &["push", "-q", "origin", "spare"]);
    let spare_git = git(&f.root.join("spare"), &["rev-parse", "--absolute-git-dir"]);
    for name in ["HEAD", "index"] {
        fs::File::options()
            .write(true)
            .open(Path::new(spare_git.trim()).join(name))
            .unwrap()
            .set_times(
                fs::FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(90000)),
            )
            .unwrap();
    }
    let named = f.root.join("topic").canonicalize().unwrap();
    let out = run_with_authority(
        &Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            targets: &[],
            remote_targets: &[],
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            include_merged: false,
            delete_remote: false,
        },
        |_, _| {
            Ok((
                Protection {
                    guards: Guards::default(),
                    claims: Claims {
                        paths: vec![],
                        branches: vec![],
                        unbound_runs: 0,
                    },
                    units: vec![],
                    unit_refs: vec![UnitRef {
                        exact: true,
                        unit: "plangraph-md-sync.service".into(),
                        path: named.join("tools/run.sh"),
                    }],
                    cwds: vec![],
                },
                vec![Pull {
                    number: 7,
                    state: "MERGED".into(),
                    sha: f.sha.clone(),
                }],
            ))
        },
    )
    .unwrap();
    assert!(f.root.join("topic").exists(), "{out}");
    assert!(
        out.contains("named by user unit plangraph-md-sync.service"),
        "{out}"
    );
    assert!(!f.root.join("spare").exists(), "{out}");
}

#[test]
fn sweep_keeps_a_worktree_named_through_a_dangling_intermediate_link() {
    use std::os::unix::fs::symlink;
    let f = Fixture::new();
    let topic = f.root.join("topic").canonicalize().unwrap();
    let units = f.state.join("systemd/user");
    fs::create_dir_all(&units).unwrap();
    // example.service -> alias/unit.service, alias -> <topic>/bin, and bin does not exist.
    fs::create_dir_all(f.state.join("links")).unwrap();
    symlink(topic.join("bin"), f.state.join("links/alias")).unwrap();
    symlink(
        f.state.join("links/alias/unit.service"),
        units.join("example.service"),
    )
    .unwrap();
    let unit_refs = pg_worker_core::unit_refs::unit_file_refs(&units, Path::new("/h")).unwrap();
    let out = run_with_authority(
        &Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            targets: &[],
            remote_targets: &[],
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            include_merged: false,
            delete_remote: false,
        },
        |_, _| {
            Ok((
                Protection {
                    guards: Guards::default(),
                    claims: Claims {
                        paths: vec![],
                        branches: vec![],
                        unbound_runs: 0,
                    },
                    units: vec![],
                    unit_refs: unit_refs.clone(),
                    cwds: vec![],
                },
                vec![Pull {
                    number: 7,
                    state: "MERGED".into(),
                    sha: f.sha.clone(),
                }],
            ))
        },
    )
    .unwrap();
    assert!(topic.exists(), "{out}");
    assert!(out.contains("example.service"), "{out}");
}
