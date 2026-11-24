//! The proof a part commit that git skips in a rebase is already on main, byte for byte in effect.
use super::Host;
use std::io::Write;
use std::process::Stdio;

impl Host {
    /// The non-merge commits of HEAD that git would skip when replaying them onto `onto`, because
    /// `onto` holds a commit with the same patch id.
    pub(super) fn skipped_commits(&self, onto: &str) -> Result<Vec<String>, String> {
        let all = self.commits(&format!("{onto}..HEAD"), &[])?;
        let kept = self.commits(
            &format!("{onto}...HEAD"),
            &["--cherry-pick", "--right-only"],
        )?;
        Ok(all.into_iter().filter(|c| !kept.contains(c)).collect())
    }

    /// Refuse the rebase, before it starts, when git would skip a part commit without a main commit
    /// that makes the same change at the same place. A patch id ignores whitespace and the place of
    /// a change, so the id alone does not show that the change is on main.
    pub(super) fn refuse_unproven_drops(&self, onto: &str) -> Result<(), String> {
        let skipped = self.skipped_commits(onto)?;
        if skipped.is_empty() {
            return Ok(());
        }
        let upstream_commits = self.commits(&format!("HEAD..{onto}"), &[])?;
        let upstream = self.patch_ids(&upstream_commits)?;
        let own = self.patch_ids(&skipped)?;
        for commit in &skipped {
            if self.changes_nothing(commit)? {
                continue;
            }
            let proven = own.iter().filter(|(_, id)| id == commit).any(|(patch, _)| {
                upstream
                    .iter()
                    .filter(|(held, _)| held == patch)
                    .any(|(_, held)| self.same_change(commit, held))
            });
            if !proven {
                return Err(format!(
                    "git would skip part commit {commit} as already on main, but no main commit makes the same change at the same place; it is not dropped"
                ));
            }
        }
        Ok(())
    }

    fn commits(&self, range: &str, flags: &[&str]) -> Result<Vec<String>, String> {
        let mut args = vec!["rev-list", "--no-merges"];
        args.extend(flags);
        args.push(range);
        Ok(self.git(&args)?.lines().map(str::to_owned).collect())
    }

    /// Whether `commit` leaves the tree of its parent (the empty tree for a root) as it was.
    fn changes_nothing(&self, commit: &str) -> Result<bool, String> {
        let before = self
            .git(&[
                "rev-parse",
                "--verify",
                "-q",
                &format!("{commit}^^{{tree}}"),
            ])
            .or_else(|_| self.git(&["hash-object", "-t", "tree", "/dev/null"]))?;
        Ok(self.git(&["rev-parse", "--verify", &format!("{commit}^{{tree}}")])? == before)
    }

    /// Whether `commit` applied on the parent of `held` gives exactly the tree of `held`.
    fn same_change(&self, commit: &str, held: &str) -> bool {
        let rev = |name: String| self.git(&["rev-parse", "--verify", &name]);
        let (Ok(before), Ok(parent), Ok(expected)) = (
            rev(format!("{commit}^")),
            rev(format!("{held}^")),
            rev(format!("{held}^{{tree}}")),
        ) else {
            return false;
        };
        self.git(&[
            "merge-tree",
            "--write-tree",
            &format!("--merge-base={before}"),
            &parent,
            commit,
        ])
        .is_ok_and(|merged| merged.lines().next() == Some(expected.as_str()))
    }

    /// `(patch id, commit)` of `commits`, read from plumbing (`diff-tree --stdin | patch-id`) that
    /// no user setting such as `log.abbrevCommit` or `format.pretty` changes.
    fn patch_ids(&self, commits: &[String]) -> Result<Vec<(String, String)>, String> {
        if commits.is_empty() {
            return Ok(Vec::new());
        }
        let mut tree = self
            .command("git")
            .arg("-C")
            .arg(&self.cwd)
            .args(["diff-tree", "--stdin", "-p", "--root", "--no-renames"])
            .args(["--no-ext-diff", "--no-textconv", "--no-color"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        let mut input = tree.stdin.take().ok_or("git diff-tree has no stdin")?;
        let stream = tree.stdout.take().ok_or("git diff-tree has no stdout")?;
        // The list is written from its own thread: diff-tree's output may fill the pipe before all
        // of it is read, and patch-id only reads once it is running.
        let (ids, written) = std::thread::scope(|scope| {
            let writer = scope.spawn(move || writeln!(input, "{}", commits.join("\n")));
            let ids = Self::listing_in(
                self.command("git")
                    .arg("-C")
                    .arg(&self.cwd)
                    .arg("patch-id")
                    .stdin(Stdio::from(stream)),
            );
            (ids, writer.join())
        });
        let done = tree.wait().map_err(|e| e.to_string())?;
        if !done.success() || !matches!(written, Ok(Ok(()))) {
            return Err("git diff-tree failed".into());
        }
        Ok(ids?
            .lines()
            .filter_map(|line| line.split_once(' '))
            .map(|(patch, commit)| (patch.to_owned(), commit.to_owned()))
            .collect())
    }
}
