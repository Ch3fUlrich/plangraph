//! Git under protected inputs: no user, system or repository configuration, fixed merge settings,
//! attributes only from a recorded file. Every merge of a train and every replay goes through here.
use std::path::Path;
use std::process::{Command, Output};

/// The merge strategy and settings are constants of the policy version.
pub const POLICY_VERSION: &str = "land-policy-1";

pub fn git_version() -> Result<String, String> {
    let out = Command::new("git")
        .arg("--version")
        .output()
        .map_err(|e| e.to_string())?;
    String::from_utf8(out.stdout)
        .map(|s| s.trim().to_owned())
        .map_err(|_| "non-UTF8 git version".into())
}

/// A git command in `dir` with an isolated configuration; `attributes` is the recorded attributes file.
pub fn command(dir: &Path, attributes: Option<&Path>) -> Command {
    let mut c = Command::new("git");
    c.current_dir(dir)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", "/nonexistent")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_AUTHOR_NAME", "plangraph-train")
        .env("GIT_AUTHOR_EMAIL", "train@example.invalid")
        .env("GIT_COMMITTER_NAME", "plangraph-train")
        .env("GIT_COMMITTER_EMAIL", "train@example.invalid")
        .env("LC_ALL", "C")
        .args([
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args([
            "-c",
            "merge.renames=true",
            "-c",
            "merge.conflictStyle=merge",
        ])
        .args(["-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]);
    match attributes {
        Some(path) => c
            .arg("-c")
            .arg(format!("core.attributesFile={}", path.display())),
        None => c.args(["-c", "core.attributesFile=/dev/null"]),
    };
    c
}

pub fn run(dir: &Path, attributes: Option<&Path>, args: &[&str]) -> Result<Output, String> {
    command(dir, attributes)
        .args(args)
        .output()
        .map_err(|e| format!("git {args:?}: {e}"))
}

/// stdout of a git command that must succeed.
pub fn out(dir: &Path, args: &[&str]) -> Result<String, String> {
    let o = run(dir, None, args)?;
    if !o.status.success() {
        return Err(format!(
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&o.stdout).trim().to_owned())
}

pub fn is_ancestor(dir: &Path, old: &str, new: &str) -> Result<bool, String> {
    let o = run(dir, None, &["merge-base", "--is-ancestor", old, new])?;
    match o.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => Err(format!(
            "merge-base: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        )),
    }
}

/// Commits of `range` (`a..b`), oldest first.
pub fn commits(dir: &Path, range: &str) -> Result<Vec<String>, String> {
    Ok(out(dir, &["rev-list", "--reverse", range])?
        .lines()
        .map(str::to_owned)
        .collect())
}

/// The outcome of merging `head` onto the checkout at `dir` with the protected inputs.
pub enum Merged {
    /// A merge commit with this tree and sha.
    Clean {
        tree: String,
        commit: String,
    },
    Conflict,
}

/// Merges `head` into the detached checkout at `dir` with `--no-ff`; on conflict the merge is aborted.
pub fn merge(
    dir: &Path,
    attributes: Option<&Path>,
    head: &str,
    message: &str,
    date: &str,
) -> Result<Merged, String> {
    let mut c = command(dir, attributes);
    c.env("GIT_AUTHOR_DATE", date)
        .env("GIT_COMMITTER_DATE", date);
    let o = c
        .args(["merge", "--no-ff", "-s", "ort", "-m", message, head])
        .output()
        .map_err(|e| e.to_string())?;
    if o.status.success() {
        let commit = out(dir, &["rev-parse", "HEAD"])?;
        let tree = out(dir, &["rev-parse", "HEAD^{tree}"])?;
        return Ok(Merged::Clean { tree, commit });
    }
    let _ = run(dir, attributes, &["merge", "--abort"]);
    if String::from_utf8_lossy(&o.stdout).contains("CONFLICT")
        || String::from_utf8_lossy(&o.stderr).contains("CONFLICT")
        || o.status.code() == Some(1)
    {
        return Ok(Merged::Conflict);
    }
    Err(format!(
        "git merge failed: {}",
        String::from_utf8_lossy(&o.stderr).trim()
    ))
}

/// A detached checkout of `commit` in a private shared clone, so the repository's own `info/attributes`
/// and local configuration cannot reach a merge. Objects made here are fetched back with `adopt`.
pub struct Checkout {
    pub dir: tempfile::TempDir,
    repo: std::path::PathBuf,
}

impl Checkout {
    pub fn new(repo: &Path, commit: &str) -> Result<Checkout, String> {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let target = dir.path().to_string_lossy().into_owned();
        let source = repo.to_string_lossy().into_owned();
        out(
            dir.path(),
            &["clone", "-q", "--shared", "--no-checkout", &source, &target],
        )?;
        out(dir.path(), &["checkout", "-q", "--detach", commit])?;
        Ok(Checkout {
            dir,
            repo: repo.to_owned(),
        })
    }
    pub fn path(&self) -> &Path {
        self.dir.path()
    }
    /// Fetches `commit` (made in this checkout) into the repository under the ref `hold`.
    pub fn adopt(&self, commit: &str, hold: &str) -> Result<(), String> {
        let source = self.dir.path().to_string_lossy().into_owned();
        out(
            &self.repo,
            &["fetch", "-q", &source, &format!("{commit}:{hold}")],
        )
        .map(|_| ())
    }
}
