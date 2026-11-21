//! The registrar's rules for a part (P0, P1, P6 of the replay design): pinned heads, the stack read from base
//! refs, a child registered only on its parent's READY head, `land_contained`. The entry-predicate conditions
//! (`imported_history`, `undeclared_dependency`, `shared_history`, `.gitattributes`) are the part-entry checks'.
use super::git;
use super::train::{Own, Parent};
use std::collections::BTreeSet;
use std::path::Path;

/// One open pull request as the registrar observes it.
#[derive(Clone, Debug)]
pub struct OpenPr {
    pub pr: u64,
    pub head_branch: String,
    pub base_ref: String,
    /// The head of the current READY entry, if any.
    pub ready: Option<String>,
    /// Every head the registrar observed for this pull request, oldest first.
    pub seen: Vec<String>,
    /// The head that was consumed by a landing or by `land_contained`.
    pub consumed: Option<String>,
}

/// `deps` of a `land_ready` entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Deps {
    pub parent: Option<Parent>,
    /// The base the review of the part must have used.
    pub diff_base: String,
    pub own: Own,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The parent has no READY head or the head does not contain it.
    BehindParent,
    /// A commit of an earlier head of the parent remains in the child's own range.
    SupersededParentHistory,
    /// The base ref names neither the landing base nor an open pull request's branch.
    UnknownBase,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Registration {
    Ready(Deps),
    /// The head is already in the landing base: consumed like a landing, never READY.
    Contained,
    Refused(Refusal),
}

/// Pins the objects of an observed head (`land/seen/<pr>/<head>`), a READY head also under `land/ready`.
pub fn pin(repo: &Path, pr: u64, head: &str, ready: bool) -> Result<(), String> {
    git::out(
        repo,
        &[
            "update-ref",
            &format!("refs/heads/land/seen/{pr}/{head}"),
            head,
        ],
    )?;
    if ready {
        git::out(
            repo,
            &[
                "update-ref",
                &format!("refs/heads/land/ready/{pr}/{head}"),
                head,
            ],
        )?;
    }
    Ok(())
}

/// The READY entries the builder reads: `(pr, head)` from the `land/ready` pins.
pub fn ready_pins(repo: &Path) -> Result<Vec<(u64, String)>, String> {
    let listing = git::out(
        repo,
        &[
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads/land/ready/",
        ],
    )?;
    let mut out = Vec::new();
    for name in listing.lines() {
        let rest = name.trim_start_matches("refs/heads/land/ready/");
        if let Some((pr, head)) = rest.split_once('/') {
            if let Ok(pr) = pr.parse() {
                out.push((pr, head.to_owned()));
            }
        }
    }
    out.sort();
    Ok(out)
}

fn contains(repo: &Path, ancestor: &str, head: &str) -> Result<bool, String> {
    git::is_ancestor(repo, ancestor, head)
}

fn set(repo: &Path, range: &[&str]) -> Result<BTreeSet<String>, String> {
    let mut args = vec!["rev-list"];
    args.extend_from_slice(range);
    Ok(git::out(repo, &args)?.lines().map(str::to_owned).collect())
}

/// No commit of `q..head` may be in an earlier observed head of the parent unless it is also in `q` or the base.
fn superseded(
    repo: &Path,
    base: &str,
    q: &str,
    head: &str,
    parent_seen: &[String],
) -> Result<bool, String> {
    let own = set(repo, &[&format!("{q}..{head}")])?;
    for old in parent_seen.iter().filter(|s| s.as_str() != q) {
        let theirs = set(repo, &[old, &format!("^{q}"), &format!("^{base}")])?;
        if !theirs.is_disjoint(&own) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn deps(
    repo: &Path,
    parent: Option<Parent>,
    diff_base: String,
    head: &str,
) -> Result<Registration, String> {
    let own = git::commits(repo, &format!("{diff_base}..{head}"))?;
    Ok(Registration::Ready(Deps {
        parent,
        diff_base,
        own: Own::of(&own),
    }))
}

/// Registers `pr`'s current head. `base` is the landing base commit, `landing_ref` its branch name.
pub fn register(
    repo: &Path,
    base: &str,
    landing_ref: &str,
    pr: &OpenPr,
    head: &str,
    all: &[OpenPr],
) -> Result<Registration, String> {
    if contains(repo, head, base)? {
        return Ok(Registration::Contained);
    }
    if pr.base_ref == landing_ref {
        let diff_base = git::out(repo, &["merge-base", head, base])?;
        return deps(repo, None, diff_base, head);
    }
    let Some(parent) = all
        .iter()
        .find(|p| p.pr != pr.pr && p.head_branch == pr.base_ref)
    else {
        return Ok(Registration::Refused(Refusal::UnknownBase));
    };
    if let Some(landed) = parent
        .consumed
        .as_deref()
        .filter(|l| contains(repo, l, base).unwrap_or(false))
    {
        // The parent landed: the child is a bottom part on the landed head.
        if !contains(repo, landed, head)? {
            return Ok(Registration::Refused(Refusal::BehindParent));
        }
        if superseded(repo, base, landed, head, &parent.seen)? {
            return Ok(Registration::Refused(Refusal::SupersededParentHistory));
        }
        return deps(repo, None, landed.to_owned(), head);
    }
    let Some(q) = parent.ready.as_deref() else {
        return Ok(Registration::Refused(Refusal::BehindParent));
    };
    if !contains(repo, q, head)? {
        return Ok(Registration::Refused(Refusal::BehindParent));
    }
    if superseded(repo, base, q, head, &parent.seen)? {
        return Ok(Registration::Refused(Refusal::SupersededParentHistory));
    }
    deps(
        repo,
        Some(Parent {
            pr: parent.pr,
            head: q.to_owned(),
        }),
        q.to_owned(),
        head,
    )
}

/// When the parent registers a new head, the READY entry of every child that does not contain it ends
/// (`land_unready {behind_parent}`).
pub fn children_to_unready(
    repo: &Path,
    parent: &OpenPr,
    new_head: &str,
    children: &[(u64, String)],
) -> Result<Vec<u64>, String> {
    let _ = parent;
    let mut out = Vec::new();
    for (pr, head) in children {
        if !contains(repo, new_head, head)? {
            out.push(*pr);
        }
    }
    Ok(out)
}
