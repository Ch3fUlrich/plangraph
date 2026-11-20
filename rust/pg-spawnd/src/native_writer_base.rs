//! Pins a native run's base: the pushed ref when `origin` has it, else the host repository's own.
use super::{output, Host};

/// The object name of exactly `name` among `listing` lines of `<sha> <ref>`; prefix and suffix
/// matches of other refs are not the ref.
fn exact_ref(listing: &str, name: &str) -> Option<String> {
    listing.lines().find_map(|line| {
        let (sha, found) = line.split_once(char::is_whitespace)?;
        (found.trim() == name).then(|| sha.to_owned())
    })
}

impl Host {
    fn repo_git(&self, args: &[&str]) -> Result<String, String> {
        output(self.command("git").arg("-C").arg(&self.repo).args(args))
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
        let remote = self.repo_git(&["ls-remote", "origin", &self.base_ref])?;
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
        let local = self.repo_git(&[
            "for-each-ref",
            "--format=%(objectname) %(refname)",
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
}
