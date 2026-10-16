//! Agent harnesses: how a coding agent is invoked inside a worktree, plus the
//! sandbox pre-check that must hold before it runs.
//!
//! A harness only *builds* a command; `run_harness` hands that command to
//! `stall::supervise_with_cancel`, which owns the process group, output capture
//! and the stall/timeout/cancel kills. Keeping the two apart lets tests inspect
//! argv and cwd without spawning anything.
//!
//! The sandbox check exists because a symlink *inside* the worktree can point
//! outside it: a write through the link lands beyond the write set even though
//! the path it was written to looks confined. Such links are resolved purely
//! lexically, so a broken link is judged the same as a working one.

use pg_worker_core::stall::{Opts, Supervised, supervise_with_cancel};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

/// One agent invocation: where it runs, which model, what to do.
pub struct HarnessRun<'a> {
    pub workdir: &'a Path,
    pub model: &'a str,
    pub prompt: &'a str,
}

/// Builds the agent's command. The caller runs it with `stall::supervise_with_cancel`.
pub trait Harness: Send + Sync {
    fn name(&self) -> &str;
    fn command(&self, run: &HarnessRun) -> Command;
}

/// The `opencode run` CLI.
#[derive(Debug, Clone)]
pub struct Opencode {
    pub bin: String,
}

impl Opencode {
    pub fn new() -> Self {
        Opencode {
            bin: "opencode".into(),
        }
    }
}

impl Default for Opencode {
    fn default() -> Self {
        Self::new()
    }
}

impl Harness for Opencode {
    fn name(&self) -> &str {
        "opencode"
    }

    fn command(&self, run: &HarnessRun) -> Command {
        let mut cmd = Command::new(&self.bin);
        // No shell: the prompt is a single argv element, so nothing in it is parsed.
        cmd.arg("run")
            .arg("-m")
            .arg(run.model)
            .arg(run.prompt)
            .current_dir(run.workdir)
            .stdin(Stdio::null())
            // A self-update mid-run would replace the very binary we supervise.
            .env("OPENCODE_DISABLE_AUTOUPDATE", "1");
        cmd
    }
}

/// Build the command for `h` and run it under the stall/timeout watchdog.
///
/// `cancelled` is polled while the child runs so a cancel kills the process
/// group promptly instead of waiting for stall, timeout or natural exit.
pub fn run_harness(
    h: &dyn Harness,
    run: &HarnessRun,
    opts: &Opts,
    cancelled: &dyn Fn() -> bool,
) -> std::io::Result<Supervised> {
    supervise_with_cancel(h.command(run), opts, cancelled)
}

/// Errors from the symlink sandbox check.
#[derive(Debug, thiserror::Error)]
pub enum SandboxError {
    #[error("worktree has symlinks escaping it: {0:?}")]
    Escape(Vec<PathBuf>),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

/// Reject the worktree if any symlink in it resolves outside it.
pub fn check_sandbox(workdir: &Path) -> Result<(), SandboxError> {
    let escapes = find_escaping_symlinks(workdir)?;
    if escapes.is_empty() {
        Ok(())
    } else {
        Err(SandboxError::Escape(escapes))
    }
}

/// Every symlink under `workdir` (relative to it, sorted) whose target
/// resolves outside `workdir`.
///
/// Targets are resolved lexically, without touching the filesystem, so a broken
/// link cannot hide by pointing at a path that does not exist yet.
pub fn find_escaping_symlinks(workdir: &Path) -> std::io::Result<Vec<PathBuf>> {
    let base = lexical(&std::path::absolute(workdir)?);
    let mut out = Vec::new();
    walk(workdir, workdir, &base, true, &mut out)?;
    out.sort();
    Ok(out)
}

fn walk(
    root: &Path,
    dir: &Path,
    base: &Path,
    top: bool,
    out: &mut Vec<PathBuf>,
) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        // Only the worktree's own top-level `.git` is repo metadata, never part
        // of the write set. A `.git` deeper down may be a symlink an agent could
        // use to escape, so it must still be checked.
        if top && entry.file_name().to_str() == Some(".git") {
            continue;
        }
        let path = entry.path();
        // file_type comes from symlink_metadata: it never follows the link.
        let ft = entry.file_type()?;
        if ft.is_symlink() {
            let rel = path
                .strip_prefix(root)
                .map(Path::to_path_buf)
                .unwrap_or_else(|_| path.clone());
            let parent = base.join(rel.parent().unwrap_or(Path::new("")));
            let target = std::fs::read_link(&path)?;
            let resolved = if target.is_absolute() {
                lexical(&target)
            } else {
                lexical(&parent.join(&target))
            };
            if !resolved.starts_with(base) {
                out.push(rel);
            }
        } else if ft.is_dir() {
            walk(root, &path, base, false, out)?;
        }
    }
    Ok(())
}

/// Normalize `.` and `..` textually, with no filesystem access.
fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::Prefix(_) | Component::RootDir => out.push(c.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                // Pop a real component; `..` above a root simply stays put.
                if !out.pop() && out.as_os_str().is_empty() {
                    out.push("..");
                }
            }
            Component::Normal(s) => out.push(s),
        }
    }
    out
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::ffi::OsStr;
    use std::os::unix::fs::symlink;

    fn link(workdir: &Path, at: &str, target: impl AsRef<Path>) -> PathBuf {
        let p = workdir.join(at);
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        symlink(target, &p).unwrap();
        p
    }

    #[test]
    fn opencode_command_has_args_cwd_and_env() {
        let h = Opencode::new();
        let run = HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "some/model",
            prompt: "fix the bug",
        };
        let c = h.command(&run);
        assert_eq!(c.get_program(), OsStr::new("opencode"));
        let args: Vec<String> = c
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(args, ["run", "-m", "some/model", "fix the bug"]);
        assert_eq!(c.get_current_dir(), Some(Path::new("/tmp/w")));
        assert_eq!(
            c.get_envs()
                .find(|(k, _)| k.to_str() == Some("OPENCODE_DISABLE_AUTOUPDATE"))
                .and_then(|(_, v)| v),
            Some(OsStr::new("1"))
        );
    }

    struct Fake {
        script: &'static str,
    }

    impl Harness for Fake {
        fn name(&self) -> &str {
            "fake"
        }
        fn command(&self, _run: &HarnessRun) -> Command {
            let mut c = Command::new("sh");
            c.arg("-c").arg(self.script);
            c
        }
    }

    #[test]
    fn run_harness_supervises_and_captures_output() {
        let dir = tempfile::tempdir().unwrap();
        let run = HarnessRun {
            workdir: dir.path(),
            model: "m",
            prompt: "p",
        };
        let r = run_harness(&Fake { script: "echo hi" }, &run, &Opts::default(), &|| {
            false
        })
        .unwrap();
        assert_eq!(r.code, Some(0));
        assert!(String::from_utf8_lossy(&r.output).contains("hi"));
    }

    #[test]
    fn symlink_inside_the_worktree_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        std::fs::write(w.join("target.txt"), "x").unwrap();
        link(w, "alias", "target.txt");
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
        assert!(check_sandbox(w).is_ok());
    }

    #[test]
    fn symlink_out_of_the_worktree_escapes() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, "passwd", "/etc/passwd");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from("passwd")]
        );
        assert!(matches!(check_sandbox(w), Err(SandboxError::Escape(_))));
    }

    #[test]
    fn relative_link_climbing_out_escapes() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, "up", "../../x");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from("up")]
        );
    }

    #[test]
    fn dotdot_that_returns_inside_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, "l", "sub/../ok");
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
    }

    #[test]
    fn absolute_link_inside_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        std::fs::write(w.join("ok"), "x").unwrap();
        symlink(w.join("ok"), w.join("abs")).unwrap();
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
    }

    #[test]
    fn git_dir_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, ".git/evil", "/etc/passwd");
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
        assert!(check_sandbox(w).is_ok());
    }

    #[test]
    fn nested_git_symlink_escapes_but_top_level_git_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        // Top-level `.git` is the repo's own metadata and stays exempt...
        link(w, ".git/evil", "/etc/passwd");
        // ...but a `.git` nested below it must not borrow that exemption.
        link(w, "sub/.git", "/etc");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from("sub/.git")]
        );
        assert!(matches!(check_sandbox(w), Err(SandboxError::Escape(_))));
    }
}
