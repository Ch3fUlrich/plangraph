use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::{ffi::OsStr, fs};

#[path = "sync/refusals.rs"]
mod refusals;

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

    #[cfg(unix)]
    fn git_shim(&self, script: &str) -> Command {
        use std::os::unix::fs::PermissionsExt;
        let real = self
            .command("sh")
            .args(["-c", "command -v git"])
            .output()
            .unwrap();
        assert!(real.status.success());
        let bin = self.base.join("bin");
        fs::create_dir_all(&bin).unwrap();
        let shim = bin.join("git");
        fs::write(
            &shim,
            format!("#!/bin/sh\n{script}\nexec \"$PG_TEST_GIT\" \"$@\"\n"),
        )
        .unwrap();
        fs::set_permissions(&shim, fs::Permissions::from_mode(0o755)).unwrap();
        let mut paths = vec![bin];
        paths.extend(std::env::split_paths(&std::env::var_os("PATH").unwrap()));
        let real = String::from_utf8(real.stdout).unwrap();
        let mut command = self.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
        command
            .arg("--repo")
            .arg(&self.repo)
            .env("PATH", std::env::join_paths(paths).unwrap())
            .env("PG_TEST_GIT", real.trim());
        command
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
        let mut command = self.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
        command.arg("--repo").arg(&self.repo);
        self.alert_command(reason, behind, command);
    }

    fn alert_command(&self, reason: &str, behind: u64, mut command: Command) {
        let before = self.snapshot();
        let out = command.output().unwrap();
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

#[test]
fn untracked_and_ignored_collisions_are_not_divergence() {
    for ignored in [false, true] {
        let f = Fixture::new();
        f.commit(&f.seed, "incoming", "remote contents");
        f.git(&f.seed, &["push", "origin", "main"]);
        fs::write(f.repo.join("incoming"), "private contents").unwrap();
        if ignored {
            fs::write(f.repo.join(".git/info/exclude"), "incoming\n").unwrap();
        }
        f.alert("ff-failed", 2);
    }
}

#[cfg(unix)]
#[test]
fn symbolic_ref_errors_are_not_detached() {
    let f = Fixture::new();
    for code in [2, 128] {
        f.alert_command(
            "branch-check-failed",
            1,
            f.git_shim(&format!(
                "if [ \"$5\" = symbolic-ref ] && [ \"$8\" = HEAD ]; then exit {code}; fi"
            )),
        );
    }
}

#[cfg(unix)]
#[test]
fn default_ref_errors_do_not_guess_main() {
    let f = Fixture::new();
    let script =
        "if [ \"$5\" = symbolic-ref ] && [ \"$7\" = refs/remotes/origin/HEAD ]; then exit 128; fi";
    f.alert_command("probe-failed", 1, f.git_shim(script));
}

#[cfg(unix)]
#[test]
fn merge_base_errors_are_not_divergence() {
    let f = Fixture::new();
    f.alert_command(
        "ancestry-check-failed",
        1,
        f.git_shim("if [ \"$5\" = merge-base ]; then exit 128; fi"),
    );
}

#[cfg(unix)]
#[test]
fn discovery_skips_symlinked_repositories() {
    let mut f = Fixture::new();
    let outside = f.base.join("outside");
    fs::rename(&f.repo, &outside).unwrap();
    std::os::unix::fs::symlink(&outside, &f.repo).unwrap();
    f.repo = outside;
    let before = f.snapshot();
    for explicit_root in [false, true] {
        let mut command = f.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
        if explicit_root {
            command.arg("--root").arg(f.base.join("code"));
        }
        let out = command.output().unwrap();
        assert!(out.status.success(), "{out:?}");
        assert!(out.stdout.is_empty() && out.stderr.is_empty(), "{out:?}");
        assert_eq!(f.snapshot(), before);
        assert!(!f.repo.join(".git/FETCH_HEAD").exists());
    }
    f.fast_forwarded(f.run());
}

#[cfg(unix)]
#[test]
fn fetch_timeout_kills_remote_helper_process_group() {
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    let pid_file = f.base.join("remote-helper.pid");
    let mut command =
        f.git_shim("if [ \"$5\" = fetch ]; then sleep 120 & echo $! > \"$PG_TEST_PID\"; wait; fi");
    command.env("PG_TEST_PID", &pid_file);
    let started = Instant::now();
    f.alert_command("fetch-failed", 0, command);
    assert!(started.elapsed() < Duration::from_secs(70));
    let pid = fs::read_to_string(pid_file).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let running = loop {
        let out = f
            .command("ps")
            .args(["-o", "stat=", "-p", pid.trim()])
            .output()
            .unwrap();
        assert!(out.stderr.is_empty(), "{out:?}");
        let state = String::from_utf8(out.stdout).unwrap();
        let running =
            out.status.success() && !state.trim().is_empty() && !state.trim().starts_with('Z');
        if !running || Instant::now() >= deadline {
            break running;
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let _ = f.command("kill").args(["-KILL", pid.trim()]).output();
    assert!(!running, "remote helper survived the fetch timeout");
}
