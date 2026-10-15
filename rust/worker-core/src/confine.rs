//! Write-set confinement: what an attempt changed versus what the card allows.

use crate::git::{git_bytes, GitError};
use crate::hash::Fnv;
use globset::{Glob, GlobBuilder, GlobSet, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::Path;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Violation {
    /// File changed but matches no write-set glob.
    OutsideWriteSet { path: String },
    /// File matches a forbid glob (wins over the write set).
    Forbidden { path: String },
}

#[derive(Debug, thiserror::Error)]
pub enum ConfineError {
    #[error(transparent)]
    Git(#[from] GitError),
    #[error("bad glob {0:?}: {1}")]
    Glob(String, globset::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

fn build(patterns: &[String]) -> Result<GlobSet, ConfineError> {
    let mut b = GlobSetBuilder::new();
    for p in patterns {
        // `dir/` means everything below it.
        let pat = if p.ends_with('/') {
            format!("{p}**")
        } else {
            p.clone()
        };
        let g: Glob = GlobBuilder::new(&pat)
            .literal_separator(true)
            .build()
            .map_err(|e| ConfineError::Glob(p.clone(), e))?;
        b.add(g);
    }
    b.build()
        .map_err(|e| ConfineError::Glob(patterns.join(","), e))
}

/// Pure check of a set of changed paths against the card's globs.
/// An empty write set allows nothing.
pub fn evaluate<'a>(
    changed: impl IntoIterator<Item = &'a str>,
    write_set: &[String],
    forbid: &[String],
) -> Result<Vec<Violation>, ConfineError> {
    let (allow, deny) = (build(write_set)?, build(forbid)?);
    let mut out = Vec::new();
    for path in changed {
        let path = path.to_string();
        if deny.is_match(&path) {
            out.push(Violation::Forbidden { path });
        } else if !allow.is_match(&path) {
            out.push(Violation::OutsideWriteSet { path });
        }
    }
    Ok(out)
}

fn names(dir: &Path, args: &[&str]) -> Result<Vec<String>, GitError> {
    let raw = git_bytes(dir, args)?;
    Ok(raw
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect())
}

/// Files changed in `dir` relative to `base`: committed (base..HEAD),
/// uncommitted, and untracked (honouring .gitignore). Renames count as both paths.
pub fn changed_files(dir: &Path, base: &str) -> Result<BTreeSet<String>, GitError> {
    let range = format!("{base}..HEAD");
    let mut set = BTreeSet::new();
    for args in [
        vec!["diff", "--name-only", "-z", "--no-renames", &range],
        vec!["diff", "--name-only", "-z", "--no-renames", "HEAD"],
        vec!["ls-files", "--others", "--exclude-standard", "-z"],
    ] {
        set.extend(names(dir, &args)?);
    }
    Ok(set)
}

/// Positive confinement of everything an attempt changed. Empty result = clean.
pub fn check_confinement(
    dir: &Path,
    base: &str,
    write_set: &[String],
    forbid: &[String],
) -> Result<Vec<Violation>, ConfineError> {
    let files = changed_files(dir, base)?;
    evaluate(files.iter().map(String::as_str), write_set, forbid)
}

/// Hash of the attempt's total change versus `base` (tracked diff + untracked files).
pub fn diff_hash(dir: &Path, base: &str) -> Result<String, ConfineError> {
    let mut h = Fnv::default();
    h.update(&git_bytes(
        dir,
        &["diff", "--binary", "--no-renames", base],
    )?);
    for f in names(dir, &["ls-files", "--others", "--exclude-standard", "-z"])? {
        h.update(f.as_bytes());
        h.update(&std::fs::read(dir.join(&f))?);
    }
    Ok(h.hex())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::{commit_all, repo, write};

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn evaluate_rules() {
        let ws = s(&["src/**", "docs/", "*.toml"]);
        let fb = s(&["src/secret/**", ".github/**"]);
        let cases: &[(&str, Option<Violation>)] = &[
            ("src/a.rs", None),
            ("src/deep/b/c.rs", None),
            ("docs/x.md", None),
            ("Cargo.toml", None),
            (
                "sub/Cargo.toml",
                Some(Violation::OutsideWriteSet {
                    path: "sub/Cargo.toml".into(),
                }),
            ),
            (
                "README.md",
                Some(Violation::OutsideWriteSet {
                    path: "README.md".into(),
                }),
            ),
            (
                "src/secret/k",
                Some(Violation::Forbidden {
                    path: "src/secret/k".into(),
                }),
            ),
            (
                ".github/w.yml",
                Some(Violation::Forbidden {
                    path: ".github/w.yml".into(),
                }),
            ),
        ];
        for (p, want) in cases {
            let got = evaluate([*p], &ws, &fb).unwrap();
            assert_eq!(got, want.iter().cloned().collect::<Vec<_>>(), "{p}");
        }
    }

    #[test]
    fn empty_write_set_allows_nothing() {
        let v = evaluate(["a"], &[], &[]).unwrap();
        assert_eq!(v, vec![Violation::OutsideWriteSet { path: "a".into() }]);
        assert!(evaluate(std::iter::empty(), &[], &[]).unwrap().is_empty());
    }

    #[test]
    fn bad_glob_is_an_error() {
        assert!(evaluate(["a"], &s(&["["]), &[]).is_err());
    }

    #[test]
    fn changed_files_covers_committed_uncommitted_untracked() {
        let (dir, base) = repo();
        let p = dir.path();
        assert!(changed_files(p, &base).unwrap().is_empty());
        write(p, "src/committed.rs", "1");
        commit_all(p, "c");
        write(p, "README.md", "changed\n"); // uncommitted tracked
        write(p, "new/untracked.txt", "u"); // untracked
        let got: Vec<String> = changed_files(p, &base).unwrap().into_iter().collect();
        assert_eq!(got, ["README.md", "new/untracked.txt", "src/committed.rs"]);
    }

    #[test]
    fn check_confinement_end_to_end() {
        let (dir, base) = repo();
        let p = dir.path();
        write(p, "src/ok.rs", "1");
        commit_all(p, "c");
        write(p, "stray.txt", "x");
        write(p, "src/secret/k", "x");
        let v = check_confinement(p, &base, &s(&["src/**"]), &s(&["src/secret/**"])).unwrap();
        assert_eq!(
            v,
            vec![
                Violation::Forbidden {
                    path: "src/secret/k".into()
                },
                Violation::OutsideWriteSet {
                    path: "stray.txt".into()
                },
            ]
        );
    }

    #[test]
    fn diff_hash_tracks_content() {
        let (dir, base) = repo();
        let p = dir.path();
        let h0 = diff_hash(p, &base).unwrap();
        write(p, "a.txt", "one");
        let h1 = diff_hash(p, &base).unwrap();
        assert_ne!(h0, h1);
        assert_eq!(h1, diff_hash(p, &base).unwrap());
        write(p, "a.txt", "two");
        let h2 = diff_hash(p, &base).unwrap();
        assert_ne!(h1, h2);
        commit_all(p, "c"); // tracked diff vs base is still deterministic
        let h3 = diff_hash(p, &base).unwrap();
        assert_eq!(h3, diff_hash(p, &base).unwrap());
    }
}
