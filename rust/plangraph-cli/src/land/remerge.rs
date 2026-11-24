//! The new `origin/main` merged into an open train after another train landed (D-6774): the train keeps
//! its parts, its part reviews and its manifest; only the tip moves. The chain check allows exactly this
//! merge, replay recomputes it and the remerge-diff review reads it.
use super::build::{base_date, Attrs};
use super::git::{self, Checkout, Merged};
use super::train::Manifest;
use std::path::Path;

/// Merges `main` into the tip of `m` and returns the manifest with the new tip. `main` must be a commit
/// the train does not contain yet; a conflict is not resolved here (combine the train instead).
pub fn remerge(repo: &Path, m: &Manifest, main: &str) -> Result<Manifest, String> {
    if git::is_ancestor(repo, main, &m.train_sha)? {
        return Err(format!(
            "remerge: train {} already contains {main}",
            m.build
        ));
    }
    let attrs = Attrs::new(&m.policy.attributes)?;
    let date = base_date(repo, main)?;
    let check = Checkout::new(repo, &m.train_sha)?;
    let message = format!(
        "Merge main {} into train {}",
        &main[..12.min(main.len())],
        m.build
    );
    match git::merge(check.path(), Some(&attrs.path), main, &message, &date)? {
        Merged::Clean { commit, .. } => {
            let hold = format!("refs/land/remerge/{}/{commit}", m.build);
            check.adopt(&commit, &hold)?;
            let mut next = m.clone();
            next.train_sha = commit;
            Ok(next)
        }
        Merged::Conflict { .. } => Err(format!(
            "remerge: train {} conflicts with main {}; combine it into a train built on the new main",
            m.build,
            &main[..12.min(main.len())]
        )),
    }
}
