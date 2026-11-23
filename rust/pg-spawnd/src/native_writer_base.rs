//! Pins a native run's base: the pushed ref when `origin` has it, else the host repository's own.
use super::{output, Host};
use crate::native_writer::ReviewBase;
use std::io::Write;
use std::process::Stdio;

const ORIGIN_MAIN: &str = "refs/remotes/origin/main";

/// The object name of exactly `name` among `listing` lines of `<sha>\t<ref>`; prefix and
/// suffix matches of other refs are not the ref, and the ref bytes are compared as given.
fn exact_ref(listing: &str, name: &str) -> Option<String> {
    listing.split('\n').find_map(|line| {
        let (sha, found) = line.split_once('\t')?;
        (found == name).then(|| sha.to_owned())
    })
}

impl Host {
    fn repo_git(&self, args: &[&str]) -> Result<String, String> {
        output(self.command("git").arg("-C").arg(&self.repo).args(args))
    }
    /// Git's own output with no trimming: a ref name may end in whitespace.
    fn repo_listing(&self, args: &[&str]) -> Result<String, String> {
        let result = self
            .command("git")
            .arg("-C")
            .arg(&self.repo)
            .args(args)
            .output()
            .map_err(|e| e.to_string())?;
        if !result.status.success() {
            return Err(format!(
                "command failed: {}",
                String::from_utf8_lossy(&result.stderr)
            ));
        }
        Ok(String::from_utf8_lossy(&result.stdout).into_owned())
    }
    /// Pin `base_ref` at `pin`: the pushed ref when `origin` has exactly that ref, else the
    /// host repository's own exact ref (a lane head never pushed). A ref found in neither
    /// place is refused by name; an unreachable `origin` stays an error.
    pub(super) fn pin_base(&self, pin: &str) -> Result<(), String> {
        self.repo_git(&[
            "fetch",
            "--no-write-fetch-head",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ])?;
        let remote = self.repo_listing(&["ls-remote", "origin", &self.base_ref])?;
        if exact_ref(&remote, &self.base_ref).is_some() {
            return self
                .repo_git(&[
                    "fetch",
                    "--no-write-fetch-head",
                    "origin",
                    &format!("+{}:{pin}", self.base_ref),
                ])
                .map(drop);
        }
        let local = self.repo_listing(&[
            "for-each-ref",
            "--format=%(objectname)%09%(refname)",
            &self.base_ref,
        ])?;
        let sha = exact_ref(&local, &self.base_ref).ok_or_else(|| {
            format!(
                "base_ref {} exists neither on origin nor in the host repository",
                self.base_ref
            )
        })?;
        let commit = self.repo_git(&["rev-parse", "--verify", &format!("{sha}^{{commit}}")])?;
        self.repo_git(&["update-ref", pin, &commit]).map(drop)
    }

    fn review_main_pin(&self) -> String {
        format!("refs/plangraph/native/{}/review-main", self.run_id)
    }

    pub(super) fn admit_review_main(&self, base: &str) -> Result<(), String> {
        let main = self.repo_git(&["rev-parse", "--verify", ORIGIN_MAIN])?;
        self.repo_git(&["merge-base", "--is-ancestor", &main, base])
            .map_err(|_| "stale base: pushed ref does not contain origin/main".to_owned())?;
        self.repo_git(&["update-ref", &self.review_main_pin(), &main])
            .map(drop)
    }

    /// Rebase the package before its checks so the stamp, receipts and push all bind to one head.
    pub(super) fn prepare_review_base(&self, head: &str) -> Result<ReviewBase, String> {
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
        if self.git(&["rev-parse", "HEAD"])? != head {
            return Err(format!("worktree is not at {head}"));
        }
        self.git(&[
            "fetch",
            "--no-write-fetch-head",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ])?;
        let observed_main = self.git(&["rev-parse", "--verify", ORIGIN_MAIN])?;
        let base = self.git(&["rev-parse", "--verify", &self.base_pin()])?;
        let main = match self.git(&["rev-parse", "--verify", &self.review_main_pin()]) {
            Ok(main) => main,
            // Saved runs predating the main pin still have the original package base.
            Err(_) => self.git(&["merge-base", &base, &observed_main])?,
        };
        let current = ReviewBase {
            head: head.into(),
            base,
            mode: "current",
            main,
            observed_main,
        };
        if current.main == current.observed_main {
            return Ok(current);
        }
        self.git(&[
            "merge-base",
            "--is-ancestor",
            &current.main,
            &current.observed_main,
        ])
        .map_err(|e| format!("review base refresh refused: origin/main diverged: {e}"))?;
        self.git(&["merge-base", "--is-ancestor", &current.base, head])
            .map_err(|e| {
                format!(
                    "review base refresh refused: HEAD no longer contains the package base: {e}"
                )
            })?;
        let rebased = self.rebase_package(&current);
        match rebased {
            Ok(Some(basis)) => Ok(basis),
            result => {
                if self.git(&["rev-parse", "HEAD"])? != head {
                    self.git(&["switch", "--detach", head])?;
                }
                result.map(|_| ReviewBase {
                    mode: "conflict",
                    ..current
                })
            }
        }
    }

    fn rebase_package(&self, current: &ReviewBase) -> Result<Option<ReviewBase>, String> {
        let main = &current.observed_main;
        let base = if self.git(&["merge-base", &current.base, main])? == current.base {
            main.clone()
        } else {
            // Keep an existing lane base in the ancestry of a same-lane push.
            self.git(&["switch", "--detach", &current.base])?;
            let merged = self.git(&["-c", "rerere.enabled=false", "merge", "--no-edit", main]);
            if !self.finish_integration(merged, "MERGE_HEAD", &["merge", "--abort"])? {
                return Ok(None);
            }
            self.git(&["rev-parse", "HEAD"])?
        };
        if self.git(&["rev-parse", "HEAD"])? != current.head {
            self.git(&["switch", "--detach", &current.head])?;
        }
        if self.git(&["merge-base", &base, &current.head])? != base
            && !self.rebase(&base, &current.base)?
        {
            return Ok(None);
        }
        let head = self.git(&["rev-parse", "HEAD"])?;
        self.git(&["merge-base", "--is-ancestor", &base, &head])?;
        self.update_review_pins(&base, main)?;
        Ok(Some(ReviewBase {
            mode: if head == current.head {
                "current"
            } else {
                "rebased"
            },
            head,
            base,
            main: main.clone(),
            observed_main: main.clone(),
        }))
    }

    fn rebase(&self, onto: &str, upstream: &str) -> Result<bool, String> {
        let result = self.git(&[
            "-c",
            "rerere.enabled=false",
            "rebase",
            "--no-autostash",
            "--no-update-refs",
            "--no-fork-point",
            "--rebase-merges",
            "--reapply-cherry-picks",
            "--empty=keep",
            "--onto",
            onto,
            upstream,
        ]);
        self.finish_integration(result, "rebase-merge", &["rebase", "--abort"])
    }

    fn finish_integration(
        &self,
        result: Result<String, String>,
        state: &str,
        abort: &[&str],
    ) -> Result<bool, String> {
        let Err(error) = result else { return Ok(true) };
        let path = self.git(&["rev-parse", "--path-format=absolute", "--git-path", state])?;
        if !std::path::Path::new(&path).exists() {
            return Err(error);
        }
        let unmerged = self.git(&["ls-files", "--unmerged"]);
        self.git(abort)?;
        if unmerged?.is_empty() {
            return Err(error);
        }
        Ok(false)
    }

    fn update_review_pins(&self, base: &str, main: &str) -> Result<(), String> {
        let mut git = self
            .command("git")
            .arg("-C")
            .arg(&self.cwd)
            .args(["update-ref", "--stdin"])
            .stdin(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        let written = writeln!(
            git.stdin.take().ok_or("Git ref transaction has no stdin")?,
            "update {} {base}\nupdate {} {main}",
            self.base_pin(),
            self.review_main_pin()
        );
        let status = git.wait().map_err(|e| e.to_string())?;
        written.map_err(|e| e.to_string())?;
        if !status.success() {
            return Err(format!("review base ref transaction failed: {status}"));
        }
        Ok(())
    }
}
