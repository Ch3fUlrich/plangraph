//! The deterministic steps that are plain commands: worktree, fast checks and push. The
//! steps that need an agent or a service are supplied by `Composite`'s `rest`.

use crate::step_runner::StepActions;
use pg_worker_core::batch_steps::{
    CiClass, Claim, FastChecks, Fix, Push, Review, TrainJoin, Worktree, Write,
};
use std::path::{Path, PathBuf};

/// Runs one command in a directory; `Ok` is its stdout, `Err` its failure text.
pub trait CommandRunner {
    fn run(&mut self, dir: &Path, argv: &[String]) -> Result<String, String>;
}

pub struct SystemRunner;

impl CommandRunner for SystemRunner {
    fn run(&mut self, dir: &Path, argv: &[String]) -> Result<String, String> {
        let (program, args) = argv.split_first().ok_or("empty command")?;
        let out = std::process::Command::new(program)
            .args(args)
            .current_dir(dir)
            .output()
            .map_err(|err| format!("{program}: {err}"))?;
        let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
        if out.status.success() {
            return Ok(stdout);
        }
        let tail: Vec<_> = String::from_utf8_lossy(&out.stderr)
            .lines()
            .chain(stdout.lines())
            .map(str::to_owned)
            .collect();
        let start = tail.len().saturating_sub(5);
        Err(format!(
            "{program} {}: {}",
            out.status,
            tail[start..].join(" | ")
        ))
    }
}

#[derive(Clone, Debug)]
pub struct ShellConfig {
    /// The checkout the worktree is created from.
    pub repo_dir: PathBuf,
    pub worktree_dir: PathBuf,
    /// Lane branch of the worktree and of the pull request.
    pub branch: String,
    /// Fast checks, run in order inside the worktree; each is one argv.
    pub checks: Vec<Vec<String>>,
    pub pr_title: String,
    pub pr_body: String,
}

pub struct ShellSteps<R: CommandRunner> {
    pub config: ShellConfig,
    pub runner: R,
}

fn argv(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

impl<R: CommandRunner> ShellSteps<R> {
    /// The stamp the review gate reads: the shared receipt of the worktree for this head.
    fn stamp_path(&self, head: &str) -> Result<PathBuf, String> {
        pg_worker_core::worktree::fast_check_stamp(&self.config.worktree_dir, head)
            .map_err(|err| err.to_string())
    }

    /// Create the worktree on a new lane branch at `base`. A failed add can leave a registered
    /// worktree (a failing post-checkout hook does), so it is removed and the branch is dropped
    /// when it still sits at `base`; the retry then starts clean.
    pub fn worktree(&mut self, base: &str) -> Result<Worktree, String> {
        let dir = self.config.worktree_dir.to_string_lossy().into_owned();
        let repo = self.config.repo_dir.clone();
        let branch = self.config.branch.clone();
        let prune = argv(&["git", "worktree", "prune"]);
        self.runner.run(&repo, &prune)?;
        let add = argv(&["git", "worktree", "add", "-b", &branch, &dir, base]);
        if let Err(err) = self.runner.run(&repo, &add) {
            // Cleanup is best effort; the add error is the one to report.
            let _ = self.runner.run(
                &repo,
                &argv(&["git", "worktree", "remove", "--force", &dir]),
            );
            let _ = self.runner.run(&repo, &prune);
            let at = self.runner.run(
                &repo,
                &argv(&["git", "rev-parse", &format!("refs/heads/{branch}")]),
            );
            if at.is_ok_and(|sha| sha.trim() == base) {
                let _ = self
                    .runner
                    .run(&repo, &argv(&["git", "branch", "-D", &branch]));
            }
            return Err(err);
        }
        Ok(Worktree { worktree: dir })
    }

    /// Run every check on exactly `head`; the stamp file is written only when all pass.
    pub fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        let dir = self.config.worktree_dir.clone();
        let at = self
            .runner
            .run(&dir, &argv(&["git", "rev-parse", "HEAD"]))?;
        if at.trim() != head {
            return Err(format!("worktree is at {}, not {head}", at.trim()));
        }
        // The checks run on the files of the worktree, so they only speak for `head` when the
        // worktree holds nothing else.
        let dirty = self
            .runner
            .run(&dir, &argv(&["git", "status", "--porcelain"]))?;
        if !dirty.trim().is_empty() {
            return Err("worktree has uncommitted or untracked files".into());
        }
        for check in self.config.checks.clone() {
            self.runner.run(&dir, &check)?;
        }
        let stamp = self.stamp_path(head)?;
        std::fs::create_dir_all(stamp.parent().ok_or("stamp directory missing")?)
            .map_err(|err| err.to_string())?;
        std::fs::write(&stamp, "green\n").map_err(|err| err.to_string())?;
        Ok(FastChecks {
            stamp: stamp.to_string_lossy().into_owned(),
        })
    }

    /// Push the accepted head as a fast-forward and return the draft pull request of the branch.
    pub fn push(&mut self, head: &str, stamp: &str) -> Result<Push, String> {
        let expected = self.stamp_path(head)?;
        let green = std::fs::read_to_string(&expected)
            .is_ok_and(|text| pg_worker_core::fast_check_stamp::text_is_green(&text, head));
        if !green || Path::new(stamp) != expected {
            return Err(format!("no fast-checks stamp for {head}"));
        }
        let dir = self.config.worktree_dir.clone();
        let branch = self.config.branch.clone();
        // No force flag: the remote refuses anything but a fast-forward.
        let refspec = format!("{head}:refs/heads/{branch}");
        self.runner
            .run(&dir, &argv(&["git", "push", "origin", &refspec]))?;
        let view = argv(&[
            "gh", "pr", "view", &branch, "--json", "number", "-q", ".number",
        ]);
        let number = match self.runner.run(&dir, &view) {
            Ok(found) => found,
            Err(_) => {
                let create = argv(&[
                    "gh",
                    "pr",
                    "create",
                    "--draft",
                    "--head",
                    &branch,
                    "--title",
                    &self.config.pr_title,
                    "--body",
                    &self.config.pr_body,
                ]);
                self.runner.run(&dir, &create)?;
                self.runner.run(&dir, &view)?
            }
        };
        let pr: u64 = number
            .trim()
            .parse()
            .map_err(|_| format!("no pull request number in {number:?}"))?;
        Ok(Push { pr })
    }
}

/// The deterministic shell steps plus an implementation of everything else.
pub struct Composite<R: CommandRunner, A: StepActions> {
    pub shell: ShellSteps<R>,
    pub rest: A,
}

impl<R: CommandRunner, A: StepActions> StepActions for Composite<R, A> {
    fn claim(&mut self, items: &[String]) -> Result<Claim, String> {
        self.rest.claim(items)
    }
    fn worktree(&mut self, _claim: &str, base: &str) -> Result<Worktree, String> {
        self.shell.worktree(base)
    }
    fn write(&mut self, worktree: &str, brief: &str) -> Result<Write, String> {
        self.rest.write(worktree, brief)
    }
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        self.shell.fast_checks(head)
    }
    fn review(&mut self, head: &str, stamp: &str) -> Result<Review, String> {
        self.rest.review(head, stamp)
    }
    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        self.rest.fix(head, session, review)
    }
    fn push(&mut self, head: &str, stamp: &str, _review: &str) -> Result<Push, String> {
        self.shell.push(head, stamp)
    }
    fn ci_class(&mut self, head: &str, pr: u64) -> Result<CiClass, String> {
        self.rest.ci_class(head, pr)
    }
    fn train_join(
        &mut self,
        head: &str,
        pr: u64,
        run: u64,
        attempt: u32,
    ) -> Result<TrainJoin, String> {
        self.rest.train_join(head, pr, run, attempt)
    }
}
