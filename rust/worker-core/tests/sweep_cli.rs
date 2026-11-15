#![cfg(unix)]
use pg_worker_core::{
    disk_hygiene::Guards,
    merged_sweep::{run_with_authority, Options},
    sweep_facts::{Claims, Protection, Pull},
};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime},
};
fn git(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .current_dir(repo)
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
    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pg-worktree-sweep"));
        command
            .current_dir(&self.repo)
            .arg("--repo")
            .arg(&self.repo)
            .arg("--root")
            .arg(&self.root)
            .arg("--state")
            .arg(&self.state)
            .arg("--pins")
            .arg(self.state.join("pins.json"))
            .env_remove("PG_SWEEP_CLAIMS_READER");
        command
    }
}
#[test]
fn sweep_cli_dry_run_then_apply_saves_restore_and_keeps_remote() {
    let f = Fixture::new();
    let execute = |apply| {
        run_with_authority(
            &Options {
                repo: &f.repo,
                roots: std::slice::from_ref(&f.root),
                targets: &[],
                remote_targets: &[],
                idle_minutes: 60,
                state: &f.state,
                pins: &f.state.join("pins.json"),
                claims_reader: None,
                apply,
                delete_remote: false,
            },
            |_, _| {
                Ok((
                    Protection {
                        guards: Guards::default(),
                        claims: Claims {
                            paths: vec![],
                            branches: vec![],
                        },
                        units: vec![],
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
        .unwrap()
    };
    let out = execute(false);
    assert!(out.contains("would-remove"), "{out}");
    assert!(f.root.join("topic").exists());
    assert!(!f.state.join("sweep").exists());
    let out = execute(true);
    assert!(out.contains("removed"), "{out}");
    assert!(!f.root.join("topic").exists());
    let restore = fs::read_dir(f.state.join("sweep"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let rows = fs::read_to_string(restore).unwrap();
    assert!(rows.starts_with("repo\tpath\tbranch\tsha\tPR\n"));
    assert!(rows.contains(&format!("\trefs/heads/topic\t{}\t7\n", f.sha)));
    assert!(git(&f.repo, &["branch", "--list", "topic"])
        .trim()
        .is_empty());
    assert!(git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]).contains(&f.sha));
}
#[test]
fn sweep_cli_missing_claim_authority_protects_and_daily_marker_skips() {
    let f = Fixture::new();
    let out = f.command().arg("--apply").output().unwrap();
    assert!(out.status.success());
    assert!(f.root.join("topic").exists());
    assert!(String::from_utf8_lossy(&out.stdout).contains("claims reader unavailable"));
    fs::write(f.state.join("merged-sweep.last"), "completed").unwrap();
    let out = f.command().args(["--daily", "--apply"]).output().unwrap();
    assert!(out.status.success());
    assert!(out.stdout.is_empty());
    assert!(f.root.join("topic").exists());
}
#[test]
fn sweep_apply_restore_failure_prevents_removal_and_remote_requires_merged_pr() {
    for state in ["CLOSED", "MERGED"] {
        let f = Fixture::new();
        let opts = Options {
            repo: &f.repo,
            roots: std::slice::from_ref(&f.root),
            targets: &[],
            remote_targets: &[],
            idle_minutes: 60,
            state: &f.state,
            pins: &f.state.join("pins.json"),
            claims_reader: None,
            apply: true,
            delete_remote: true,
        };
        let mut authority = |_: &Options<'_>, _: &str| {
            Ok((
                Protection {
                    guards: Guards::default(),
                    claims: Claims {
                        paths: vec![],
                        branches: vec![],
                    },
                    units: vec![],
                    cwds: vec![],
                },
                vec![Pull {
                    number: 7,
                    state: state.into(),
                    sha: f.sha.clone(),
                }],
            ))
        };
        fs::write(f.state.join("sweep"), "not a directory").unwrap();
        assert!(run_with_authority(&opts, &mut authority).is_err());
        assert!(f.root.join("topic").exists());
        fs::remove_file(f.state.join("sweep")).unwrap();
        let report = run_with_authority(&opts, &mut authority).unwrap();
        assert!(report.contains("removed"), "{report}");
        assert!(!f.root.join("topic").exists());
        let restore = fs::read_dir(f.state.join("sweep"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        assert!(fs::read_to_string(restore)
            .unwrap()
            .contains(&format!("\t{}\t7\n", f.sha)));
        let remote = git(&f.repo, &["ls-remote", "--heads", "origin", "topic"]);
        assert_eq!(remote.is_empty(), state == "MERGED");
    }
}
