//! A secret-free child environment for everything the worker spawns on an
//! agent's behalf: the harness and the card's check command.
//!
//! The worker process holds credentials (`PG_*`, `TEMPORAL_*`, keeper and TLS
//! settings, provider keys). A coding agent or a check script running in an
//! attempt worktree must not inherit them, so the confine helpers below clear
//! the child's environment and re-add only an exact-name allowlist ([`PASS`],
//! never a prefix: a `CLAUDE_CODE_` prefix once forwarded
//! `CLAUDE_CODE_OAUTH_TOKEN`), plus a private per-attempt `HOME` so tools
//! cannot read the operator's dotfiles. Both the harness ([`confine_harness`])
//! and the card's check command ([`confine_check`]) get [`PASS`] only: harness
//! auth never travels by environment — [`HomeGuard::stage_harness_auth`]
//! copies it into the harness's private HOME as files.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Names copied from the worker's environment, when set. Exact names only:
/// a prefix would forward whatever secret the CLI happens to store under it
/// (`CLAUDE_CODE_OAUTH_TOKEN`, `OPENCODE_API_KEY`, ...).
///
/// `PATH`, `LANG` and `TERM` are what any tool needs to run at all.
/// `CARGO_HOME` and `RUSTUP_HOME` are toolchain *locations* (not secrets): with
/// `HOME` private, a Rust check could not find its toolchain without them.
pub const PASS: &[&str] = &["PATH", "LANG", "TERM", "CARGO_HOME", "RUSTUP_HOME"];

/// True when `name` may reach the harness from the worker's environment:
/// [`PASS`] only, exact names, no prefixes.
pub fn is_passed(name: &str) -> bool {
    PASS.contains(&name)
}

/// True when `name` may reach a check child: [`PASS`] only.
pub fn is_passed_check(name: &str) -> bool {
    PASS.contains(&name)
}

/// Confine a harness child (and its `--version` probe): [`PASS`], a private
/// `HOME`, and what the caller set on `cmd`. Harness auth is staged into
/// `home` as files ([`HomeGuard::stage_harness_auth`]), never passed by
/// environment.
pub fn confine_harness(cmd: &mut Command, home: &Path) {
    confine_with(cmd, home, is_passed);
}

/// Confine a card check child: [`PASS`] only, a private `HOME`, and what the
/// caller set on `cmd`. Agent credentials never reach it.
pub fn confine_check(cmd: &mut Command, home: &Path) {
    confine_with(cmd, home, is_passed_check);
}

/// Replace `cmd`'s environment with the names `pass` accepts, a private `HOME`,
/// and the variables the caller set on `cmd` explicitly (the caller's own by
/// construction, for example `OPENCODE_DISABLE_AUTOUPDATE` or a test's marker).
fn confine_with(cmd: &mut Command, home: &Path, pass: fn(&str) -> bool) {
    let explicit: Vec<(OsString, OsString)> = cmd
        .get_envs()
        .filter_map(|(k, v)| v.map(|v| (k.to_os_string(), v.to_os_string())))
        .collect();
    cmd.env_clear();
    for (k, v) in std::env::vars_os() {
        if k.to_str().is_some_and(pass) {
            cmd.env(k, v);
        }
    }
    for (k, v) in explicit {
        cmd.env(k, v);
    }
    cmd.env("HOME", home);
}

/// The private `HOME` of the attempt slot `dir`: a sibling named `<dir>.home`,
/// next to the worktree rather than in it so it never shows up as an
/// untracked change in the attempt's diff.
pub fn home_for(dir: &Path) -> PathBuf {
    home_with_suffix(dir, ".home")
}

/// The private `HOME` of the card check child of slot `dir`: `<dir>.home-check`.
/// Never the harness HOME: a harness may persist a login there.
pub fn check_home_for(dir: &Path) -> PathBuf {
    home_with_suffix(dir, ".home-check")
}

fn home_with_suffix(dir: &Path, suffix: &str) -> PathBuf {
    let mut name = dir
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_else(|| OsString::from("attempt"));
    name.push(suffix);
    dir.with_file_name(name)
}

/// Creates a private `HOME` and removes it on drop, unless it already existed
/// (an enclosing attempt owns it then). Files staged into the home via
/// [`HomeGuard::stage_harness_auth`] are removed on drop either way.
pub struct HomeGuard {
    path: PathBuf,
    owned: bool,
    staged: Vec<PathBuf>,
}

impl HomeGuard {
    pub fn for_dir(dir: &Path) -> std::io::Result<HomeGuard> {
        Self::at(home_for(dir))
    }

    /// The check child gets its own HOME (see [`check_home_for`]).
    pub fn for_check(dir: &Path) -> std::io::Result<HomeGuard> {
        Self::at(check_home_for(dir))
    }

    fn at(path: PathBuf) -> std::io::Result<HomeGuard> {
        let owned = !path.exists();
        std::fs::create_dir_all(&path)?;
        Ok(HomeGuard {
            path,
            owned,
            staged: Vec::new(),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Copy the auth files named by [`HARNESS_AUTH_DIR_ENV`] into this home,
    /// preserving relative paths. Regular files are created mode 0600 and
    /// directories mode 0700 from the outset (unix): the bytes are written
    /// into the privately-opened file, never copied world-readable and
    /// chmod-ed afterwards. One level of subdirectories is copied; the walk
    /// is never recursive and never follows a symlink — a symlink in the
    /// source is refused outright, so an auth tree is plain files or nothing
    /// is staged. Unset or a missing directory is a no-op: an auth-less
    /// harness run (a test) is normal. Everything staged is recorded and
    /// removed on drop, even when the home pre-existed and is not taken as a
    /// whole.
    pub fn stage_harness_auth(&mut self) -> std::io::Result<()> {
        stage_auth_into(&self.path, &mut self.staged)
    }
}

impl Drop for HomeGuard {
    fn drop(&mut self) {
        if self.owned {
            let _ = std::fs::remove_dir_all(&self.path);
            return;
        }
        // A pre-existing home stays, but the auth files we put in it do not.
        for staged in &self.staged {
            let _ = std::fs::remove_file(staged);
        }
    }
}

/// Env var naming a directory of harness auth files (agent CLI login state and
/// config) that the worker stages into each attempt's private HOME. A `PG_*`
/// name, so it never reaches a child; the harness reads its credentials from
/// the staged files, never from the environment.
pub const HARNESS_AUTH_DIR_ENV: &str = "PG_HARNESS_AUTH_DIR";

/// Body of [`HomeGuard::stage_harness_auth`]: stages each file and records it
/// so the guard can undo it.
fn stage_auth_into(home: &Path, staged: &mut Vec<PathBuf>) -> std::io::Result<()> {
    let Some(dir) = std::env::var_os(HARNESS_AUTH_DIR_ENV) else {
        return Ok(());
    };
    let src = PathBuf::from(dir);
    if !src.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(&src)? {
        let entry = entry?;
        // file_type comes from symlink_metadata: it never follows the link.
        let ft = entry.file_type()?;
        if ft.is_symlink() {
            return Err(symlink_refused(&entry.path()));
        }
        if ft.is_file() {
            stage_file(&entry.path(), &home.join(entry.file_name()), staged)?;
        } else if ft.is_dir() {
            // One level of subdirectories, no deeper: everything below is
            // skipped rather than recursed into.
            let dest = home.join(entry.file_name());
            create_staged_dir(&dest)?;
            for sub in std::fs::read_dir(entry.path())? {
                let sub = sub?;
                let sft = sub.file_type()?;
                if sft.is_symlink() {
                    return Err(symlink_refused(&sub.path()));
                }
                if sft.is_file() {
                    stage_file(&sub.path(), &dest.join(sub.file_name()), staged)?;
                }
            }
        }
    }
    Ok(())
}

fn symlink_refused(path: &Path) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidInput,
        format!(
            "{}: harness auth entries must not be symlinks",
            path.display()
        ),
    )
}

/// Create a staged directory at its final mode 0700 (unix) up front, so no
/// window exists between creation and the private mode taking effect. A
/// pre-existing directory is forced to 0700 as well, since `mode(0o700)` only
/// applies to a directory `create` actually makes. A pre-existing symlink at
/// `path` is refused outright: the 0700 fix-up follows symlinks and would
/// change the mode of a foreign target.
fn create_staged_dir(path: &Path) -> std::io::Result<()> {
    if std::fs::symlink_metadata(path).is_ok_and(|md| md.file_type().is_symlink()) {
        return Err(symlink_refused(path));
    }
    let mut b = std::fs::DirBuilder::new();
    b.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        b.mode(0o700);
    }
    b.create(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// Copy `from` into a freshly created `to` opened at mode 0600 (unix) before
/// any byte lands, then record it for the guard's drop. `create_new` also
/// refuses to overwrite a same-named file that predates the staging.
fn stage_file(from: &Path, to: &Path, staged: &mut Vec<PathBuf>) -> std::io::Result<()> {
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut dest = opts.open(to)?;
    // Recorded before the copy: create_new succeeded, so the file is ours and
    // the guard must remove it even if the copy below fails half-way.
    staged.push(to.to_path_buf());
    let mut src = std::fs::File::open(from)?;
    std::io::copy(&mut src, &mut dest)?;
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    /// Run `env` through `confine` and return the child's variables.
    fn child_env(prep: impl FnOnce(&mut Command), home: &Path) -> Vec<String> {
        child_env_with(confine_harness, prep, home)
    }

    fn child_env_with(
        confine: fn(&mut Command, &Path),
        prep: impl FnOnce(&mut Command),
        home: &Path,
    ) -> Vec<String> {
        let mut c = Command::new("env");
        prep(&mut c);
        confine(&mut c, home);
        let out = c.output().expect("env runs");
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(str::to_string)
            .collect()
    }

    #[test]
    fn allowlist_is_exact_names_only() {
        for n in ["PATH", "LANG", "TERM", "CARGO_HOME", "RUSTUP_HOME"] {
            assert!(is_passed(n) && is_passed_check(n), "{n} must pass");
        }
        for n in [
            // The harness sets this on its own Command; it survives via
            // `explicit`, it is not forwarded from the worker env.
            "OPENCODE_DISABLE_AUTOUPDATE",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENCODE_API_KEY",
            "PG_KEEPER_URL",
            "TEMPORAL_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "HOME",
        ] {
            assert!(!is_passed(n), "{n} must not pass");
            assert!(!is_passed_check(n), "{n} must not reach a check child");
        }
    }

    #[test]
    fn allowlist_never_names_a_secret() {
        // The allowlist is an env-forwarding policy: one TOKEN/KEY/... name in
        // PASS and the whole confinement story collapses.
        for n in PASS {
            let upper = n.to_ascii_uppercase();
            for word in ["TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL"] {
                assert!(!upper.contains(word), "PASS entry {n} names a secret");
            }
        }
    }

    #[test]
    #[serial_test::serial(env)]
    fn child_sees_allowlist_explicit_vars_and_private_home_only() {
        let home = tempfile::tempdir().unwrap();
        // A secret-looking variable in the worker's own environment. The name
        // is unique to this test, so no other test reads or sets it.
        std::env::set_var("PG_SENTINEL_SECRET_SANDBOX_ENV", "hunter2");
        let lines = child_env(
            |c| {
                c.env("PG_MARK", "explicit");
            },
            home.path(),
        );
        assert!(
            !lines
                .iter()
                .any(|l| l.contains("PG_SENTINEL_SECRET_SANDBOX_ENV") || l.contains("hunter2")),
            "worker secret leaked into the child: {lines:?}"
        );
        assert!(lines.contains(&"PG_MARK=explicit".to_string()), "{lines:?}");
        assert!(
            lines.contains(&format!("HOME={}", home.path().display())),
            "{lines:?}"
        );
        assert!(lines.iter().any(|l| l.starts_with("PATH=")), "{lines:?}");
        // Nothing outside the allowlist survives: every name is passed,
        // explicit or HOME.
        for l in &lines {
            let name = l.split('=').next().unwrap();
            assert!(
                is_passed(name) || name == "HOME" || name == "PG_MARK",
                "unexpected {name} in child env"
            );
        }
    }

    #[test]
    #[serial_test::serial(env)]
    fn agent_credentials_reach_neither_harness_nor_check_child() {
        let home = tempfile::tempdir().unwrap();
        // Sentinel values unique to this test; no other test reads them.
        let secrets: [(&str, &str); 6] = [
            ("CLAUDE_CODE_OAUTH_TOKEN", "sentinel-wf8-oauth"),
            ("CLAUDE_CODE_MESSAGING_TOKEN", "sentinel-wf8-messaging"),
            ("OPENCODE_API_KEY", "sentinel-wf8-opkey"),
            ("OPENCODE_FOO_SECRET", "sentinel-wf8-foosec"),
            ("ANTHROPIC_API_KEY", "sentinel-wf8-anth"),
            ("MY_PASSWORD", "sentinel-wf8-pw"),
        ];
        for (name, value) in &secrets {
            std::env::set_var(name, value);
        }
        for confine in [confine_harness, confine_check] {
            let lines = child_env_with(confine, |_| {}, home.path());
            for (name, value) in &secrets {
                assert!(
                    !lines.iter().any(|l| {
                        l.starts_with(format!("{name}=").as_str()) || l.contains(*value)
                    }),
                    "{name} leaked into a confined child: {lines:?}"
                );
            }
        }
    }

    #[test]
    fn home_guard_creates_and_removes_only_what_it_owns() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("c1-1");
        assert_eq!(home_for(&dir), root.path().join("c1-1.home"));
        {
            let g = HomeGuard::for_dir(&dir).unwrap();
            assert!(g.path().is_dir());
            // A nested guard (a check inside an attempt) must not remove it.
            drop(HomeGuard::for_dir(&dir).unwrap());
            assert!(g.path().is_dir());
        }
        assert!(!home_for(&dir).exists());
    }

    #[test]
    fn check_home_is_separate_from_the_harness_home() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("c1-1");
        let h = HomeGuard::for_dir(&dir).unwrap();
        std::fs::write(h.path().join("login-token"), "secret").unwrap();
        let c = HomeGuard::for_check(&dir).unwrap();
        assert_ne!(h.path(), c.path());
        assert_eq!(c.path(), root.path().join("c1-1.home-check"));
        assert!(!c.path().join("login-token").exists());
        drop(c);
        assert!(!check_home_for(&dir).exists());
        assert!(h.path().join("login-token").exists());
    }

    #[test]
    #[serial_test::serial(env)]
    fn stage_harness_auth_stages_plain_files_and_refuses_symlinks() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let src = root.path().join("auth");
        let dir = root.path().join("c1-3");
        // Unset is a no-op: an auth-less harness run stays empty.
        std::env::remove_var(HARNESS_AUTH_DIR_ENV);
        let mut g = HomeGuard::for_dir(&dir).unwrap();
        g.stage_harness_auth().unwrap();
        assert_eq!(std::fs::read_dir(g.path()).unwrap().count(), 0);
        drop(g);

        std::fs::create_dir_all(src.join("sub")).unwrap();
        std::fs::write(src.join("credentials.json"), "sentinel-wf8-auth").unwrap();
        std::fs::write(src.join("sub").join("login"), "x").unwrap();
        std::env::set_var(HARNESS_AUTH_DIR_ENV, &src);
        {
            let mut g = HomeGuard::for_dir(&dir).unwrap();
            g.stage_harness_auth().unwrap();
            let f = g.path().join("credentials.json");
            assert_eq!(std::fs::read_to_string(&f).unwrap(), "sentinel-wf8-auth");
            assert_eq!(
                std::fs::metadata(&f).unwrap().permissions().mode() & 0o777,
                0o600
            );
            let d = g.path().join("sub");
            assert_eq!(
                std::fs::metadata(&d).unwrap().permissions().mode() & 0o777,
                0o700
            );
            assert!(d.join("login").exists());
            // A confined child sees the staged file through its private HOME.
            let mut c = Command::new("sh");
            c.arg("-c").arg(r#"cat "$HOME/credentials.json""#);
            confine_harness(&mut c, g.path());
            let out = c.output().expect("sh runs");
            assert_eq!(String::from_utf8_lossy(&out.stdout), "sentinel-wf8-auth");
            // Dropping the guard below takes the staged auth with the home.
            drop(g);
            assert!(!home_for(&dir).exists());
            assert!(!home_for(&dir).join("credentials.json").exists());
        }

        // A symlink anywhere in the source tree is refused outright: staging
        // must not hand the agent a path outside the auth dir.
        std::env::set_var(HARNESS_AUTH_DIR_ENV, &src);
        std::os::unix::fs::symlink("/etc/passwd", src.join("evil")).unwrap();
        let mut g = HomeGuard::for_dir(&dir).unwrap();
        assert!(g.stage_harness_auth().is_err());
        std::env::remove_var(HARNESS_AUTH_DIR_ENV);
    }

    #[test]
    fn stage_file_records_the_file_when_the_copy_fails() {
        let root = tempfile::tempdir().unwrap();
        let dest = root.path().join("credentials.json");
        let mut staged = Vec::new();
        // A missing source fails the copy, but the opened dest is already ours.
        assert!(stage_file(&root.path().join("nonexistent"), &dest, &mut staged).is_err());
        assert_eq!(staged, vec![dest.clone()]);
        assert!(dest.exists());
        assert_eq!(std::fs::metadata(&dest).unwrap().len(), 0);
    }

    #[test]
    fn create_staged_dir_forces_0700_on_a_preexisting_dir() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("sub");
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        create_staged_dir(&dir).unwrap();
        assert_eq!(
            std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn create_staged_dir_refuses_a_symlinked_dir() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("target");
        std::fs::create_dir(&target).unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o755)).unwrap();
        let link = root.path().join("link");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        // The 0700 fix-up follows symlinks, so a linked-to directory must be
        // refused rather than having a foreign target's mode changed.
        assert!(create_staged_dir(&link).is_err());
        assert_eq!(
            std::fs::metadata(&target).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }

    #[test]
    #[serial_test::serial(env)]
    fn staged_auth_is_removed_even_from_a_pre_existing_home() {
        let root = tempfile::tempdir().unwrap();
        let src = root.path().join("auth");
        let dir = root.path().join("c1-4");
        std::fs::create_dir_all(&src).unwrap();
        std::fs::write(src.join("credentials.json"), "sentinel-wf8-auth").unwrap();
        std::env::set_var(HARNESS_AUTH_DIR_ENV, &src);
        // The home pre-exists (an enclosing attempt owns it), so the guard
        // must not take it down — but everything it staged must still go.
        let home = home_for(&dir);
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(home.join("operator-file"), "keep").unwrap();
        {
            let mut g = HomeGuard::for_dir(&dir).unwrap();
            g.stage_harness_auth().unwrap();
            assert!(home.join("credentials.json").exists());
        }
        std::env::remove_var(HARNESS_AUTH_DIR_ENV);
        assert!(!home.join("credentials.json").exists());
        assert!(home.join("operator-file").exists());
        assert!(home.is_dir());
    }
}
