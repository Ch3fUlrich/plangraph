//! Pins a native run's base: the pushed ref when `origin` has it, else the host repository's own.
use super::{output, Host};
use crate::native_writer::ReviewBase;
use std::collections::HashSet;
use std::io::Write;
use std::process::Stdio;

/// A merge or rebase either finished or stopped on these unmerged paths (at least one).
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Integrated {
    Done,
    Conflict(Vec<String>),
}

enum Rebased {
    Done(ReviewBase),
    Conflict(Vec<String>),
}

const ORIGIN_MAIN: &str = "refs/remotes/origin/main";

/// The object name of exactly `name` among `listing` lines of `<sha>\t<ref>`; prefix and
/// suffix matches of other refs are not the ref, and the ref bytes are compared as given.
fn exact_ref(listing: &str, name: &str) -> Option<String> {
    listing.split('\n').find_map(|line| {
        let (sha, found) = line.split_once('\t')?;
        (found == name).then(|| sha.to_owned())
    })
}

/// A path as text without loss: a backslash is `\\`, and each byte that is not valid UTF-8 is
/// `\xNN`, so two different paths never give the same text.
pub(super) fn lossless_path(bytes: &[u8]) -> String {
    let mut text = String::new();
    let mut rest = bytes;
    while !rest.is_empty() {
        let (valid, bad) = match std::str::from_utf8(rest) {
            Ok(valid) => (valid, 0),
            Err(e) => (
                std::str::from_utf8(&rest[..e.valid_up_to()]).unwrap_or_default(),
                e.error_len().unwrap_or(rest.len() - e.valid_up_to()),
            ),
        };
        text.push_str(&valid.replace('\\', "\\\\"));
        let end = valid.len() + bad;
        for byte in &rest[valid.len()..end] {
            text.push_str(&format!("\\x{byte:02x}"));
        }
        rest = &rest[end..];
    }
    text
}

impl Host {
    fn repo_git(&self, args: &[&str]) -> Result<String, String> {
        output(self.command("git").arg("-C").arg(&self.repo).args(args))
    }
    /// Git's own output with no trimming: a ref name may end in whitespace.
    fn repo_listing(&self, args: &[&str]) -> Result<String, String> {
        Self::listing_in(self.command("git").arg("-C").arg(&self.repo).args(args))
    }
    fn listing_in(command: &mut std::process::Command) -> Result<String, String> {
        Self::bytes_in(command).map(|bytes| String::from_utf8_lossy(&bytes).into_owned())
    }
    /// Git's stdout as the raw bytes, for output that carries file names.
    fn bytes_in(command: &mut std::process::Command) -> Result<Vec<u8>, String> {
        let result = command.output().map_err(|e| e.to_string())?;
        if !result.status.success() {
            return Err(format!(
                "command failed: {}",
                String::from_utf8_lossy(&result.stderr)
            ));
        }
        Ok(result.stdout)
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
            conflict_files: Vec::new(),
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
            Ok(Rebased::Done(basis)) => Ok(basis),
            result => {
                if self.git(&["rev-parse", "HEAD"])? != head {
                    self.git(&["switch", "--detach", head])?;
                }
                result.map(|r| ReviewBase {
                    mode: "conflict",
                    conflict_files: match r {
                        Rebased::Conflict(files) => files,
                        Rebased::Done(_) => Vec::new(),
                    },
                    ..current
                })
            }
        }
    }

    fn rebase_package(&self, current: &ReviewBase) -> Result<Rebased, String> {
        let main = &current.observed_main;
        let base = if self.git(&["merge-base", &current.base, main])? == current.base {
            main.clone()
        } else {
            // Keep an existing lane base in the ancestry of a same-lane push.
            self.git(&["switch", "--detach", &current.base])?;
            let merged = self.git(&["-c", "rerere.enabled=false", "merge", "--no-edit", main]);
            if let Integrated::Conflict(files) =
                self.finish_integration(merged, "MERGE_HEAD", &["merge", "--abort"])?
            {
                return Ok(Rebased::Conflict(files));
            }
            self.git(&["rev-parse", "HEAD"])?
        };
        if self.git(&["rev-parse", "HEAD"])? != current.head {
            self.git(&["switch", "--detach", &current.head])?;
        }
        if self.git(&["merge-base", &base, &current.head])? != base {
            if let Integrated::Conflict(files) = self.rebase(&base)? {
                return Ok(Rebased::Conflict(files));
            }
        }
        let head = self.git(&["rev-parse", "HEAD"])?;
        self.git(&["merge-base", "--is-ancestor", &base, &head])?;
        self.update_review_pins(&base, main)?;
        Ok(Rebased::Done(ReviewBase {
            mode: if head == current.head {
                "current"
            } else {
                "rebased"
            },
            head,
            base,
            main: main.clone(),
            observed_main: main.clone(),
            conflict_files: Vec::new(),
        }))
    }

    /// Replay the commits of HEAD that `onto` does not hold on `onto`. `onto` is also the upstream,
    /// so a patch `onto` already holds is found by its patch id and dropped before any replay
    /// (a replay of it next to a later edit on `onto` would otherwise conflict). Git's patch id
    /// ignores whitespace, so a drop is allowed only where the patch also matches a commit of
    /// `onto` byte for byte; any other match is refused before the rebase starts.
    pub(super) fn rebase(&self, onto: &str) -> Result<Integrated, String> {
        self.refuse_whitespace_only_matches(onto)?;
        let result = self.git(&[
            "-c",
            "rerere.enabled=false",
            "rebase",
            "--no-autostash",
            "--no-update-refs",
            "--no-fork-point",
            "--rebase-merges",
            "--empty=drop",
            onto,
        ]);
        self.finish_integration(result, "rebase-merge", &["rebase", "--abort"])
    }

    fn refuse_whitespace_only_matches(&self, onto: &str) -> Result<(), String> {
        let lines = |args: &[&str]| -> Result<Vec<String>, String> {
            Ok(self.git(args)?.lines().map(str::to_owned).collect())
        };
        let own = format!("{onto}..HEAD");
        let symmetric = format!("{onto}...HEAD");
        let all = lines(&["rev-list", "--no-merges", &own])?;
        let kept = lines(&[
            "rev-list",
            "--no-merges",
            "--cherry-pick",
            "--right-only",
            &symmetric,
        ])?;
        let dropped: Vec<&String> = all.iter().filter(|c| !kept.contains(c)).collect();
        if dropped.is_empty() {
            return Ok(());
        }
        let upstream: HashSet<String> = self
            .verbatim_patch_ids(&format!("HEAD..{onto}"))?
            .into_iter()
            .map(|(patch, _)| patch)
            .collect();
        let own_ids = self.verbatim_patch_ids(&own)?;
        for commit in dropped {
            // A commit with no diff has no patch id: dropping it loses nothing.
            let mut ids = own_ids.iter().filter(|(_, id)| id == commit).peekable();
            if ids.peek().is_some() && !ids.any(|(patch, _)| upstream.contains(patch)) {
                return Err(format!(
                    "part commit {commit} matches a commit of main only up to whitespace; it is not dropped"
                ));
            }
        }
        Ok(())
    }

    /// `(patch id, commit)` of the non-merge commits of `range`, the patch id taken with the
    /// whitespace kept (`git patch-id --verbatim`).
    fn verbatim_patch_ids(&self, range: &str) -> Result<Vec<(String, String)>, String> {
        let mut log = self
            .command("git")
            .arg("-C")
            .arg(&self.cwd)
            .args(["log", "-p", "--no-merges", "--no-renames", "--no-ext-diff"])
            .args(["--no-textconv", "--no-color", range])
            .stdout(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        let stream = log.stdout.take().ok_or("git log has no stdout")?;
        let ids = Self::listing_in(
            self.command("git")
                .arg("-C")
                .arg(&self.cwd)
                .args(["patch-id", "--verbatim"])
                .stdin(Stdio::from(stream)),
        );
        if !log.wait().map_err(|e| e.to_string())?.success() {
            return Err(format!("git log {range} failed"));
        }
        Ok(ids?
            .lines()
            .filter_map(|line| line.split_once(' '))
            .map(|(patch, commit)| (patch.to_owned(), commit.to_owned()))
            .collect())
    }

    pub(super) fn finish_integration(
        &self,
        result: Result<String, String>,
        state: &str,
        abort: &[&str],
    ) -> Result<Integrated, String> {
        let Err(error) = result else {
            return Ok(Integrated::Done);
        };
        let path = self.git(&["rev-parse", "--path-format=absolute", "--git-path", state])?;
        if !std::path::Path::new(&path).exists() {
            return Err(error);
        }
        // The unmerged paths are read before the abort clears them; a failure without one is no conflict.
        // NUL-delimited and untrimmed, so a path git would quote or end in whitespace is exact.
        let unmerged = Self::bytes_in(self.command("git").arg("-C").arg(&self.cwd).args([
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=U",
        ]));
        self.git(abort)?;
        let files: Vec<String> = unmerged?
            .split(|byte| *byte == 0)
            .filter(|path| !path.is_empty())
            .map(lossless_path)
            .collect();
        if files.is_empty() {
            return Err(error);
        }
        Ok(Integrated::Conflict(files))
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
