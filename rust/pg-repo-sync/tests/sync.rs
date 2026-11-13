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
        Self::at(PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(name))
    }
    fn at(base: PathBuf) -> Self {
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(base.join("code")).unwrap();
        let f = Self {
            repo: base.join("code/repo"),
            seed: base.join("seed"),
            origin: base.join("origin.git"),
            base,
        };
        let (remote, seed, repo) = (
            f.origin.to_str().unwrap(),
            f.seed.to_str().unwrap(),
            f.repo.to_str().unwrap(),
        );
        f.git(&f.base, &["init", "--initial-branch=main", seed]);
        f.commit(&f.seed, "tracked", "initial");
        f.git(&f.base, &["clone", "--bare", seed, remote]);
        f.git(&f.base, &["clone", remote, repo]);
        f.commit(&f.seed, "tracked", "remote update");
        f.git(&f.seed, &["push", remote, "main"]);
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
    fn cli(&self) -> Command {
        self.command(env!("CARGO_BIN_EXE_pg-repo-sync"))
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
    fn run(&self, args: &[&str], status: Option<i32>) -> String {
        let mut c = self.cli();
        c.args(args);
        checked(c, status)
    }
    fn alert(&self, reason: &str) {
        let state = || {
            [
                self.git(&self.repo, &["rev-parse", "HEAD"]),
                fs::read(self.repo.join("tracked")).unwrap(),
            ]
        };
        let before = state();
        let expected = format!("ALERT code/repo reason={reason}\n");
        assert_eq!(self.run(&["--repo", "code/repo"], Some(1)), expected);
        assert_eq!(state(), before);
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.base);
    }
}
fn checked(mut command: Command, expected: Option<i32>) -> String {
    let out = command.output().unwrap();
    assert_eq!(out.status.code(), expected, "{out:?}");
    assert!(out.stderr.is_empty(), "{out:?}");
    let text = String::from_utf8(out.stdout).unwrap();
    for line in text.lines() {
        let alert = line.strip_prefix("ALERT ").unwrap();
        let (_, reason) = alert.split_once(" reason=").unwrap();
        let branch = reason.strip_prefix("other-branch:").unwrap_or("");
        let other = !branch.is_empty() && !branch.chars().any(char::is_whitespace);
        assert!(
            ["detached", "tracked-changes", "diverged", "fetch-failed"].contains(&reason) || other,
            "{line}"
        );
    }
    assert!(!text.contains("behind="));
    text
}
macro_rules! cases {
    ($($(#[$attr:meta])* $name:ident($f:ident) $body:block)*) => {$ (
        $(#[$attr])* #[test]
        fn $name() { let $f = Fixture::new(); $body }
    )*};
}
cases! {
    clean_behind_main_is_fast_forwarded_silently(f) {
        assert_eq!(f.run(&[], Some(0)), "");
        assert_eq!(f.git(&f.repo, &["rev-parse", "HEAD"]), f.git(&f.seed, &["rev-parse", "HEAD"]));
        assert_eq!(fs::read_to_string(f.repo.join("tracked")).unwrap(), "remote update");
        assert_eq!(f.run(&[], Some(0)), "");
    }
    modified_tracked_file_alerts_without_changes(f) {
        fs::write(f.repo.join("tracked"), "local edits").unwrap();
        f.alert("tracked-changes"); f.git(&f.repo, &["add", "tracked"]); f.alert("tracked-changes");
    }
    detached_head_alerts_without_changes(f) { f.git(&f.repo, &["switch", "--detach"]); f.alert("detached"); }
    feature_branch_alerts_without_changes(f) { f.git(&f.repo, &["switch", "-c", "feature"]); f.alert("other-branch:feature"); }
    diverged_main_alerts_without_changes(f) { f.commit(&f.repo, "local", "local commit"); f.alert("diverged"); }
    fetch_failure_alerts_without_changes(f) { fs::rename(&f.origin, f.base.join("unavailable.git")).unwrap(); f.alert("fetch-failed"); }
    explicit_repo_preserves_cli_label(f) {
        f.git(&f.repo, &["switch", "--detach"]);
        let label = "./code/../code/repo";
        assert_eq!(f.run(&["--repo", label], Some(1)), format!("ALERT {label} reason=detached\n"));
    }
    missing_home_and_missing_code_are_silent(f) {
        let mut c = f.cli(); c.env_remove("HOME"); assert_eq!(checked(c, Some(0)), "");
        fs::rename(f.base.join("code"), f.base.join("elsewhere")).unwrap();
        assert_eq!(f.run(&[], Some(0)), "");
    }
    #[cfg(unix)] discovery_direct_children_git_entries_and_symlinks(f) {
        use std::os::unix::fs::symlink;
        f.git(&f.repo, &["switch", "--detach"]);
        let linked = f.base.join("code/linked-lanes");
        f.git(&f.repo, &["worktree", "add", "--detach", linked.to_str().unwrap(), "HEAD"]);
        symlink(&f.repo, f.base.join("code/alias-worktrees")).unwrap();
        fs::create_dir_all(f.base.join("code/container/nested/.git")).unwrap();
        fs::create_dir(f.base.join("code/dangling")).unwrap();
        symlink("missing", f.base.join("code/dangling/.git")).unwrap();
        let expected = "ALERT alias-worktrees reason=detached\nALERT dangling reason=fetch-failed\nALERT linked-lanes reason=detached\nALERT repo reason=detached\n";
        assert_eq!(f.run(&[], Some(1)), expected);
    }
    #[cfg(unix)] invalid_git_entries_never_fetch_or_merge_the_parent(f) {
        use std::os::unix::fs::symlink;
        fs::rename(f.base.join("code"), f.base.join("original-code")).unwrap();
        f.git(&f.base, &["clone", f.origin.to_str().unwrap(), "code"]);
        let parent = f.base.join("code");
        f.commit(&f.seed, "tracked", "new parent update");
        f.git(&f.seed, &["push", f.origin.to_str().unwrap(), "main"]);
        for name in ["dangling", "empty", "invalid"] { fs::create_dir(parent.join(name)).unwrap(); }
        symlink("missing", parent.join("dangling/.git")).unwrap();
        fs::create_dir(parent.join("empty/.git")).unwrap();
        fs::write(parent.join("invalid/.git"), "not a git directory").unwrap();
        let state = || (f.git(&parent, &["rev-parse", "HEAD", "origin/main"]),
            fs::read(parent.join("tracked")).unwrap(), fs::read(parent.join(".git/FETCH_HEAD")).ok());
        let before = state();
        let expected = "ALERT dangling reason=fetch-failed\nALERT empty reason=fetch-failed\nALERT invalid reason=fetch-failed\n";
        assert_eq!(f.run(&[], Some(1)), expected);
        assert_eq!(state(), before);
        for name in ["dangling", "empty", "invalid"] {
            let label = format!("code/{name}");
            assert_eq!(f.run(&["--repo", &label], Some(1)), format!("ALERT {label} reason=fetch-failed\n"));
            assert_eq!(state(), before);
        }
    }
    #[cfg(unix)] defensive_probe_failures_use_fetch_failed(f) {
        use std::os::unix::fs::PermissionsExt;
        let real = f.command("sh").args(["-c", "command -v git"]).output().unwrap();
        assert!(real.status.success()); let real = String::from_utf8(real.stdout).unwrap();
        let bin = f.base.join("bin"); fs::create_dir(&bin).unwrap();
        for probe in ["symbolic-ref:refs/remotes/origin/HEAD", "symbolic-ref:--short", "rev-list:*", "status:*"] {
            fs::write(bin.join("git"), format!("#!/bin/sh\n[ \"$4\" = core.hooksPath=/dev/null ] && [ \"$6\" = core.fsmonitor=false ] || exit 99\ncase \"$7:$9\" in {probe}) touch \"$PG_TEST_HIT\"; exit 128;; esac\nexec \"$PG_TEST_GIT\" \"$@\"\n")).unwrap();
            fs::set_permissions(bin.join("git"), fs::Permissions::from_mode(0o755)).unwrap();
            let mut paths = vec![bin.clone()]; paths.extend(env::split_paths(&env::var_os("PATH").unwrap()));
            let mut c = f.cli(); c.args(["--repo", "code/repo"]).env("PATH", env::join_paths(paths).unwrap())
                .env("PG_TEST_GIT", real.trim()).env("PG_TEST_HIT", bin.join("hit"));
            assert_eq!(checked(c, Some(1)), "ALERT code/repo reason=fetch-failed\n");
            assert!(bin.join("hit").is_file(), "probe not isolated: {probe}"); fs::remove_file(bin.join("hit")).unwrap();
        }
    }
    #[cfg(unix)] exit_status_contract_rejects_output_coupled_codes(f) {
        for (script, expected) in [("printf 'ALERT repo reason=detached\\n'; exit 1", Some(0)), ("exit 0", Some(1))] {
            assert!(std::panic::catch_unwind(|| { let mut c = f.command("sh"); c.args(["-c", script]); checked(c, expected) }).is_err());
        }
    }
    invalid_arguments_exit_two_with_one_line_usage(f) {
        for args in [vec!["--unknown"], vec!["--repo"], vec!["--repo", "code/repo", "--unknown"]] {
            let out = f.cli().args(args).output().unwrap();
            assert_eq!(out.status.code(), Some(2), "{out:?}"); assert!(out.stdout.is_empty());
            assert_eq!(out.stderr, b"Usage: pg-repo-sync [--repo PATH]...\n");
        }
    }
    #[cfg(unix)] discovery_labels_cannot_forge_alerts_or_mangle_bytes(f) {
        use std::os::unix::ffi::OsStringExt;
        f.git(&f.repo, &["switch", "--detach"]);
        let unicode = ['\u{2028}', '\u{2029}', '\u{202a}', '\u{202b}', '\u{202c}', '\u{202d}', '\u{202e}', '\u{2066}', '\u{2067}', '\u{2068}', '\u{2069}'];
        let labels = [(b"line\nALERT forged".to_vec(), "line\\nALERT forged"), (b"bad\xff".to_vec(), "bad\\xff"), (b"slash\\tab\t\r\x1b".to_vec(), "slash\\\\tab\\t\\r\\u{1b}")]
            .into_iter().map(|(raw, escaped)| (raw, escaped.to_owned()))
            .chain(unicode.into_iter().map(|ch| (format!("unicode{ch}").into_bytes(), format!("unicode{}", ch.escape_default()))));
        for (raw, escaped) in labels {
            let path = f.base.join("code").join(std::ffi::OsString::from_vec(raw)); fs::rename(&f.repo, &path).unwrap();
            assert_eq!(f.run(&[], Some(1)), format!("ALERT {escaped} reason=detached\n"));
            let mut c = f.cli(); c.arg("--repo").arg(&path);
            assert_eq!(checked(c, Some(1)), format!("ALERT {}/code/{escaped} reason=detached\n", f.base.display()));
            fs::rename(path, &f.repo).unwrap();
        }
    }
    fixture_recreates_stale_base_and_ignores_cleanup_errors(f) {
        fs::write(f.base.join("stale"), "stale").unwrap(); let fresh = Fixture::at(f.base.clone());
        assert!(!fresh.base.join("stale").exists()); drop(fresh);
        let base = f.base.clone();
        fs::write(&base, "cleanup obstruction").unwrap();
        assert_eq!(fs::remove_dir_all(&base).unwrap_err().kind(), std::io::ErrorKind::NotADirectory);
        drop(f);
        assert_eq!(fs::read(&base).unwrap(), b"cleanup obstruction");
        fs::remove_file(base).unwrap();
    }
}

#[test]
fn repo_sync_docs_use_one_short_line() {
    let lines: Vec<_> = include_str!("../../../docs/fleet-sync.md")
        .lines()
        .skip(2)
        .take_while(|line| !line.starts_with("Run `python3"))
        .filter(|line| !line.is_empty())
        .collect();
    assert_eq!(lines.len(), 1);
    assert!(lines[0].starts_with("`pg-repo-sync"));
    assert!(lines[0].len() <= 120);
}
