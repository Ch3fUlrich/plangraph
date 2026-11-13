use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::{env, fs};
static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Fixture {
    base: PathBuf,
    repo: PathBuf,
    seed: PathBuf,
    origin: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let name = format!("pg-repo-sync-{}-{id}", std::process::id());
        let base = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(name);
        fs::create_dir_all(base.join("code")).unwrap();
        let f = Self {
            repo: base.join("code/repo"),
            seed: base.join("seed"),
            origin: base.join("origin.git"),
            base,
        };
        let remote = f.origin.to_str().unwrap();
        let seed = f.seed.to_str().unwrap();
        let repo = f.repo.to_str().unwrap();
        let init = ["init", "--bare", "--initial-branch=main", remote];
        f.git(&f.base, &init);
        f.git(&f.base, &["clone", remote, seed]);
        f.commit(&f.seed, "tracked", "initial");
        f.git(&f.seed, &["push", "origin", "main"]);
        f.git(&f.base, &["clone", remote, repo]);
        f.commit(&f.seed, "tracked", "remote update");
        f.git(&f.seed, &["push", "origin", "main"]);
        f
    }
    fn command(&self, program: &str) -> Command {
        let mut c = Command::new(program);
        c.env_clear()
            .env("PATH", env::var_os("PATH").unwrap())
            .env("HOME", &self.base)
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@invalid")
            .env("GIT_COMMITTER_EMAIL", "fixture@invalid")
            .current_dir(&self.base);
        c
    }
    fn git(&self, dir: &Path, args: &[&str]) -> Vec<u8> {
        let mut c = self.command("git");
        c.args(["-c", "commit.gpgsign=false", "-C"]).arg(dir);
        let out = c.args(args).output().unwrap();
        assert!(out.status.success(), "git {args:?}: {out:?}");
        out.stdout
    }
    fn commit(&self, dir: &Path, name: &str, content: &str) {
        fs::write(dir.join(name), content).unwrap();
        self.git(dir, &["add", name]);
        self.git(dir, &["commit", "-m", "fixture"]);
    }
    fn run(&self, args: &[&str]) -> String {
        let mut c = self.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
        c.args(args);
        checked(c)
    }
    fn alert(&self, reason: &str) {
        let before = self.state();
        let expected = format!("ALERT code/repo reason={reason}\n");
        assert_eq!(self.run(&["--repo", "code/repo"]), expected);
        assert_eq!(self.state(), before);
    }
    fn state(&self) -> [Vec<u8>; 2] {
        [
            self.git(&self.repo, &["rev-parse", "HEAD"]),
            fs::read(self.repo.join("tracked")).unwrap(),
        ]
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.base).unwrap();
    }
}
fn checked(mut command: Command) -> String {
    let out = command.output().unwrap();
    let status = i32::from(!out.stdout.is_empty());
    assert_eq!(out.status.code(), Some(status), "{out:?}");
    assert!(out.stderr.is_empty(), "{out:?}");
    let text = String::from_utf8(out.stdout).unwrap();
    for line in text.lines() {
        let alert = line.strip_prefix("ALERT ").unwrap();
        let (_, reason) = alert.split_once(" reason=").unwrap();
        let branch = reason.strip_prefix("other-branch:").unwrap_or("");
        let other = !branch.is_empty() && !branch.chars().any(char::is_whitespace);
        let closed = ["detached", "tracked-changes", "diverged", "fetch-failed"];
        assert!(closed.contains(&reason) || other, "{line}");
    }
    assert!(!text.contains("behind="));
    text
}
#[test]
fn clean_behind_main_is_fast_forwarded_silently() {
    let f = Fixture::new();
    assert_eq!(f.run(&[]), "");
    let head = f.git(&f.repo, &["rev-parse", "HEAD"]);
    assert_eq!(head, f.git(&f.seed, &["rev-parse", "HEAD"]));
    let tracked = fs::read_to_string(f.repo.join("tracked")).unwrap();
    assert_eq!(tracked, "remote update");
    assert_eq!(f.run(&[]), "");
}
#[test]
fn modified_tracked_file_alerts_without_changes() {
    let f = Fixture::new();
    fs::write(f.repo.join("tracked"), "local edits").unwrap();
    f.alert("tracked-changes");
    f.git(&f.repo, &["add", "tracked"]);
    f.alert("tracked-changes");
}
#[test]
fn detached_head_alerts_without_changes() {
    let f = Fixture::new();
    f.git(&f.repo, &["switch", "--detach"]);
    f.alert("detached");
}
#[test]
fn feature_branch_alerts_without_changes() {
    let f = Fixture::new();
    f.git(&f.repo, &["switch", "-c", "feature"]);
    f.alert("other-branch:feature");
}
#[test]
fn diverged_main_alerts_without_changes() {
    let f = Fixture::new();
    f.commit(&f.repo, "local", "local commit");
    f.alert("diverged");
}
#[test]
fn fetch_failure_alerts_without_changes() {
    let f = Fixture::new();
    fs::rename(&f.origin, f.base.join("unavailable.git")).unwrap();
    f.alert("fetch-failed");
}
#[test]
fn explicit_repo_preserves_cli_label() {
    let f = Fixture::new();
    f.git(&f.repo, &["switch", "--detach"]);
    let label = "./code/../code/repo";
    let expected = format!("ALERT {label} reason=detached\n");
    assert_eq!(f.run(&["--repo", label]), expected);
}
#[test]
fn missing_home_and_missing_code_are_silent() {
    let f = Fixture::new();
    let mut c = f.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
    c.env_remove("HOME");
    assert_eq!(checked(c), "");
    fs::rename(f.base.join("code"), f.base.join("elsewhere")).unwrap();
    assert_eq!(f.run(&[]), "");
}
#[cfg(unix)]
#[test]
fn discovery_direct_children_git_entries_and_symlinks() {
    let f = Fixture::new();
    f.git(&f.repo, &["switch", "--detach"]);
    let linked = f.base.join("code/linked-lanes");
    let path = linked.to_str().unwrap();
    f.git(&f.repo, &["worktree", "add", "--detach", path, "HEAD"]);
    std::os::unix::fs::symlink(&f.repo, f.base.join("code/alias-worktrees")).unwrap();
    fs::create_dir_all(f.base.join("code/container/nested/.git")).unwrap();
    let mut lines: Vec<_> = f.run(&[]).lines().map(str::to_owned).collect();
    lines.sort();
    let labels = ["alias-worktrees", "linked-lanes", "repo"];
    let expected = labels.map(|label| format!("ALERT {label} reason=detached"));
    assert_eq!(lines, expected);
}
#[cfg(unix)]
#[test]
fn defensive_probe_failures_use_fetch_failed() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let mut lookup = f.command("sh");
    let real = lookup.args(["-c", "command -v git"]).output().unwrap();
    assert!(real.status.success());
    let real = String::from_utf8(real.stdout).unwrap();
    let bin = f.base.join("bin");
    fs::create_dir(&bin).unwrap();
    for probe in ["symbolic-ref", "rev-list", "status"] {
        fs::write(bin.join("git"), format!("#!/bin/sh\nif [ \"$5\" = {probe} ]; then exit 128; fi\nexec \"$PG_TEST_GIT\" \"$@\"\n")).unwrap();
        fs::set_permissions(bin.join("git"), fs::Permissions::from_mode(0o755)).unwrap();
        let mut paths = vec![bin.clone()];
        paths.extend(env::split_paths(&env::var_os("PATH").unwrap()));
        let mut c = f.command(env!("CARGO_BIN_EXE_pg-repo-sync"));
        c.args(["--repo", "code/repo"])
            .env("PATH", env::join_paths(paths).unwrap())
            .env("PG_TEST_GIT", real.trim());
        assert_eq!(checked(c), "ALERT code/repo reason=fetch-failed\n");
    }
}
