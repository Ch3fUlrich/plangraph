//! The new `origin/main` merged into an open train after another train landed (D-6774): the train keeps
//! its parts, its part reviews and its manifest; only the tip moves. The chain check allows exactly this
//! merge, replay recomputes it and the remerge-diff review reads it.
use super::build::{base_date, verify_manifest_with, Attrs};
use super::cli::write;
use super::git::{self, Checkout, Merged};
use super::push;
use super::resolve::Adopted;
use super::train::{Manifest, PartIn};
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

/// `remerge` followed by the replay proof under the same adopted resolutions and protected paths as
/// `land-train verify`, so a train that carries an adopted resolution can be remerged too.
pub fn remerge_verified(
    repo: &Path,
    m: &Manifest,
    main: &str,
    parts: &[PartIn],
    adopted: &[Adopted],
    protected: &[String],
) -> Result<Manifest, String> {
    let next = remerge(repo, m, main)?;
    verify_manifest_with(repo, &next, parts, adopted, protected)?;
    Ok(next)
}

/// Moves the train branch from the head the manifest `before` names to `next`, after the proof. The branch
/// moves by compare-and-swap, so a run that advanced it first wins and this one stops; the new push
/// manifest and the output file are written beside the live ones first and replace them after the move,
/// so a failed write leaves the old branch with its old manifest.
pub fn publish_swap(
    repo: &Path,
    branch: &str,
    before: &Manifest,
    next: &Manifest,
    out: &Path,
) -> Result<(), String> {
    let mut name = out.as_os_str().to_owned();
    name.push(".next");
    let staged = std::path::PathBuf::from(name);
    let train_branch = push::is_train_branch(branch);
    if train_branch {
        push::stage_manifest(repo, branch, next)?;
    }
    write(&staged, next)?;
    git::out(
        repo,
        &[
            "update-ref",
            &format!("refs/heads/{branch}"),
            &next.train_sha,
            &before.train_sha,
        ],
    )
    .map_err(|e| {
        format!(
            "remerge: {branch} no longer holds {}: {e}",
            before.train_sha
        )
    })?;
    if train_branch {
        push::promote_manifest(repo, branch)?;
    }
    std::fs::rename(&staged, out).map_err(|e| {
        format!(
            "remerge: {e}; move {} over {}",
            staged.display(),
            out.display()
        )
    })
}
