use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::{ffi::OsStr, fs};

static NEXT: AtomicUsize = AtomicUsize::new(0);

struct Fixture {
    base: PathBuf,
    repo: PathBuf,
    seed: PathBuf,
    origin: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let base = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
            "pg-repo-sync-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(base.join("code")).unwrap();
        let f = Self {
            repo: base.join("code/repo"),
            seed: base.join("seed"),
            origin: base.join("origin.git"),
            base,
        };
        f.git(
            &f.base,
            &[
                "init",
                "--bare",
                "--initial-branch=main",
                f.origin.to_str().unwrap(),
            ],
        );
        f.git(
            &f.base,
            &[
                "clone",
                f.origin.to_str().unwrap(),
                f.seed.to_str().unwrap(),
            ],
        );
        f.commit(&f.seed, "tracked", "initial");
        f.git(&f.seed, &["push", "origin", "main"]);
        f.git(
            &f.base,
            &[
                "clone",
                f.origin.to_str().unwrap(),
                f.repo.to_str().unwrap(),
            ],
        );
        f.commit(&f.seed, "tracked", "remote update");
        f.git(&f.seed, &["push", "origin", "main"]);
        f
    }

    fn command(&self, program: impl AsRef<OsStr>) -> Command {
        let mut command = Command::new(program);
        command
            .env("HOME", &self.base)
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_OPTIONAL_LOCKS", "0")
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@invalid")
            .env("GIT_COMMITTER_EMAIL", "fixture@invalid");
        for key in [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
        ] {
            command.env_remove(key);
        }
        command
    }

    fn git(&self, dir: &Path, args: &[&str]) -> Vec<u8> {
        let out = self
            .command("git")
            .args(["-c", "commit.gpgsign=false", "-C"])
            .arg(dir)
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "git {args:?}: {out:?}");
        out.stdout
    }

    fn commit(&self, dir: &Path, name: &str, content: &str) {
        fs::write(dir.join(name), content).unwrap();
        self.git(dir, &["add", name]);
        self.git(dir, &["commit", "-m", "fixture"]);
    }

    fn run(&self) -> Output {
        self.command(env!("CARGO_BIN_EXE_pg-repo-sync"))
            .arg("--repo")
            .arg(&self.repo)
            .output()
            .unwrap()
    }

    fn snapshot(&self) -> (Vec<u8>, Vec<u8>, BTreeMap<PathBuf, Vec<u8>>) {
        fn visit(dir: &Path, files: &mut BTreeMap<PathBuf, Vec<u8>>) {
            for entry in fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.file_name().unwrap() == ".git" {
                    continue;
                }
                if path.is_dir() {
                    visit(&path, files);
                } else {
                    files.insert(path.clone(), fs::read(path).unwrap());
                }
            }
        }
        let mut files = BTreeMap::new();
        visit(&self.repo, &mut files);
        (
            self.git(&self.repo, &["rev-parse", "HEAD"]),
            fs::read(self.repo.join(".git/index")).unwrap(),
            files,
        )
    }

    fn alert(&self, reason: &str, behind: u64) {
        let before = self.snapshot();
        let out = self.run();
        assert_eq!(out.status.code(), Some(1), "{out:?}");
        assert_eq!(
            out.stdout,
            format!("ALERT repo behind={behind} reason={reason}\n").as_bytes()
        );
        assert!(out.stderr.is_empty(), "{out:?}");
        assert_eq!(self.snapshot(), before);
    }

    fn fast_forwarded(&self, out: Output) {
        assert!(out.status.success(), "{out:?}");
        assert!(out.stdout.is_empty(), "{out:?}");
        assert!(out.stderr.is_empty(), "{out:?}");
        assert_eq!(
            self.git(&self.repo, &["rev-parse", "HEAD"]),
            self.git(&self.seed, &["rev-parse", "HEAD"])
        );
        assert_eq!(
            fs::read_to_string(self.repo.join("tracked")).unwrap(),
            "remote update"
        );
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.base).unwrap();
    }
}

#[test]
fn clean_behind_main_is_fast_forwarded_silently() {
    let f = Fixture::new();
    f.fast_forwarded(f.run());
}

#[test]
fn modified_tracked_file_alerts_without_changes() {
    let f = Fixture::new();
    fs::write(f.repo.join("tracked"), "local edits").unwrap();
    f.alert("tracked-changes", 1);
    f.git(&f.repo, &["add", "tracked"]);
    f.alert("tracked-changes", 1);
}

#[test]
fn detached_head_alerts_without_changes() {
    let f = Fixture::new();
    f.git(&f.repo, &["switch", "--detach"]);
    f.alert("detached", 1);
}

#[test]
fn feature_branch_alerts_without_changes() {
    let f = Fixture::new();
    f.git(&f.repo, &["switch", "-c", "feature"]);
    f.alert("other-branch:feature", 1);
}

#[test]
fn diverged_main_alerts_without_changes() {
    let f = Fixture::new();
    f.commit(&f.repo, "local", "local commit");
    f.alert("diverged", 1);
}

#[test]
fn untracked_file_does_not_block_fast_forward() {
    let f = Fixture::new();
    fs::write(f.repo.join("private-untracked"), "private contents").unwrap();
    f.fast_forwarded(f.run());
    assert_eq!(
        fs::read_to_string(f.repo.join("private-untracked")).unwrap(),
        "private contents"
    );
}

#[test]
fn fetch_failure_alerts_without_changes() {
    let f = Fixture::new();
    fs::rename(&f.origin, f.base.join("unavailable.git")).unwrap();
    f.alert("fetch-failed", 0);
}

#[test]
fn discovery_environment_isolation_and_verbose_deduplication() {
    let f = Fixture::new();
    for name in ["skip-worktrees", "skip-lanes", "not-a-repo"] {
        fs::create_dir_all(f.base.join("code").join(name).join(".git")).unwrap();
    }
    fs::rename(f.base.join("code/not-a-repo/.git"), f.base.join("not-git")).unwrap();
    let mut command = f.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
    for key in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
    ] {
        command.env(key, f.base.join("invalid"));
    }
    f.fast_forwarded(command.output().unwrap());
    let out = f
        .command(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .arg("--root")
        .arg(f.base.join("code"))
        .arg("--root")
        .arg(f.base.join("code"))
        .arg("--repo")
        .arg(&f.repo)
        .arg("--repo")
        .arg(&f.repo)
        .arg("--verbose")
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    assert_eq!(out.stdout, b"OK repo\n");
    assert!(out.stderr.is_empty());
}
