//! One git worktree per attempt, on branch `attempt/<card>/<n>`.

use crate::git::{check_rev, git, GitError};
use std::fmt;
use std::path::{Path, PathBuf};

const PREFIX: &str = "attempt/";

/// Identity of an attempt: a card and a 1-based attempt number.
///
/// `segment` is only set on ids parsed back from a branch name, where the card
/// is no longer known (the segment carries a one-way hash). Identity (`Eq`,
/// `Hash`, `Ord`) is the segment plus the number, so a parsed id equals the id
/// that produced the branch.
#[derive(Debug, Clone)]
pub struct AttemptId {
    pub card: String,
    pub n: u32,
    segment: Option<String>,
}

impl AttemptId {
    fn segment(&self) -> String {
        self.segment
            .clone()
            .unwrap_or_else(|| safe_segment(&self.card))
    }
}

impl PartialEq for AttemptId {
    fn eq(&self, o: &Self) -> bool {
        (self.n, self.segment()) == (o.n, o.segment())
    }
}
impl Eq for AttemptId {}
impl std::hash::Hash for AttemptId {
    fn hash<H: std::hash::Hasher>(&self, h: &mut H) {
        (self.segment(), self.n).hash(h);
    }
}
impl PartialOrd for AttemptId {
    fn partial_cmp(&self, o: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for AttemptId {
    fn cmp(&self, o: &Self) -> std::cmp::Ordering {
        (self.segment(), self.n).cmp(&(o.segment(), o.n))
    }
}

impl AttemptId {
    pub fn new(card: impl Into<String>, n: u32) -> Self {
        AttemptId {
            card: card.into(),
            n,
            segment: None,
        }
    }

    /// The branch `attempt/<segment>/<n>`; `<segment>` is [`safe_segment`] of
    /// the card, so a hostile key cannot reach outside `attempt/`.
    pub fn branch(&self) -> String {
        format!("{PREFIX}{}/{}", self.segment(), self.n)
    }

    /// The directory name of this attempt's worktree under the workroot:
    /// `<segment>-<n>`. The one place that name is built.
    pub fn dir_name(&self) -> String {
        format!("{}-{}", self.segment(), self.n)
    }

    /// Parse a short branch name (`attempt/<card>/<n>`); `None` for anything else.
    pub fn parse_branch(name: &str) -> Option<AttemptId> {
        let rest = name.strip_prefix(PREFIX)?;
        let (card, n) = rest.rsplit_once('/')?;
        if card.is_empty() {
            return None;
        }
        Some(AttemptId {
            card: card.to_string(),
            n: n.parse().ok()?,
            segment: Some(card.to_string()),
        })
    }
}

/// Longest readable part of a segment (a `-<hash>` of [`HASH_LEN`] hex digits
/// always follows).
const SEGMENT_MAX: usize = 48;

/// Hex digits of the 64-bit key hash appended to every segment (the whole
/// FNV-1a 64 value, so accidental collisions between distinct keys are out of reach).
const HASH_LEN: usize = 16;

/// `key` as one safe path/ref segment: non-empty, only `[A-Za-z0-9_-]`, no
/// `..`, no leading `-` or `.`, no trailing `.` or `.lock`. The readable part is
/// the key with every other character rewritten, and a stable 64-bit hash of the
/// ORIGINAL key is ALWAYS appended, even when nothing was rewritten. That makes
/// the mapping injective in practice: a key that equals another key's rewritten
/// form (`a/b` -> `a_b-<h>`, literal `a_b-<h>`) still gets its own suffix, so two
/// distinct keys never share a worktree or branch. NOT idempotent by design;
/// ids parsed from a branch keep their segment verbatim instead.
pub fn safe_segment(key: &str) -> String {
    let mut clean: String = key
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '_' | '-') {
                c
            } else {
                '_'
            }
        })
        .take(SEGMENT_MAX)
        .collect();
    if clean.is_empty() || clean.starts_with('-') {
        clean.insert(0, '_');
    }
    format!(
        "{clean}-{}",
        &crate::hash::hash_bytes(key.as_bytes())[..HASH_LEN]
    )
}

impl fmt::Display for AttemptId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}#{}", self.card, self.n)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttemptWorktree {
    pub id: AttemptId,
    pub path: PathBuf,
    pub head: String,
}

/// Create a worktree at `dest` for `id`, on a new branch starting at `base_sha`.
pub fn create(
    repo: &Path,
    base_sha: &str,
    dest: &Path,
    id: &AttemptId,
) -> Result<AttemptWorktree, GitError> {
    check_rev(base_sha)?;
    let dest_s = dest.to_string_lossy();
    // `--`: nothing after it can be taken for an option.
    git(
        repo,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            &id.branch(),
            "--",
            &dest_s,
            base_sha,
        ],
    )?;
    Ok(AttemptWorktree {
        id: id.clone(),
        path: dest.to_path_buf(),
        head: git(dest, &["rev-parse", "HEAD"])?,
    })
}

/// Remove a worktree (forced, discarding local changes). The branch is kept.
pub fn remove(repo: &Path, path: &Path) -> Result<(), GitError> {
    git(
        repo,
        &["worktree", "remove", "--force", &path.to_string_lossy()],
    )?;
    Ok(())
}

/// List worktrees whose checked-out branch is an attempt branch.
pub fn list(repo: &Path) -> Result<Vec<AttemptWorktree>, GitError> {
    let out = git(repo, &["worktree", "list", "--porcelain"])?;
    let mut res = Vec::new();
    for block in out.split("\n\n") {
        let (mut path, mut head, mut branch) = (None, String::new(), None);
        for line in block.lines() {
            if let Some(p) = line.strip_prefix("worktree ") {
                path = Some(PathBuf::from(p));
            } else if let Some(h) = line.strip_prefix("HEAD ") {
                head = h.to_string();
            } else if let Some(b) = line.strip_prefix("branch refs/heads/") {
                branch = Some(b.to_string());
            }
        }
        if let (Some(path), Some(b)) = (path, branch) {
            if let Some(id) = AttemptId::parse_branch(&b) {
                res.push(AttemptWorktree { id, path, head });
            }
        }
    }
    Ok(res)
}

#[cfg(test)]
mod tests {
    #[test]
    fn keeper_style_card_ids_make_valid_branches() {
        let id = super::AttemptId::new("demo:E2E-1", 2);
        let branch = id.branch();
        assert!(branch.starts_with("attempt/demo_E2E-1-"), "{branch}");
        assert!(!branch.contains(':'));
        assert_eq!(
            super::AttemptId::parse_branch(&branch).map(|a| a.branch()),
            Some(branch)
        );
    }

    use super::*;
    use crate::testutil::{commit_all, repo, write};

    #[test]
    fn branch_roundtrip() {
        let id = AttemptId::new("card-1", 2);
        let seg = safe_segment("card-1");
        assert!(seg.starts_with("card-1-"), "{seg}");
        assert_eq!(id.branch(), format!("attempt/{seg}/2"));
        // A parsed id equals the id that produced the branch, and re-renders
        // the very same branch (the segment is kept verbatim, never re-hashed).
        let back = AttemptId::parse_branch(&id.branch()).unwrap();
        assert_eq!(back, id);
        assert_eq!(back.branch(), id.branch());
        let nested = AttemptId::parse_branch("attempt/a/b/7").unwrap();
        assert_eq!((nested.card.as_str(), nested.n), ("a/b", 7));
        for bad in [
            "main",
            "attempt/x",
            "attempt//1",
            "attempt/x/y",
            "feature/x/1",
        ] {
            assert_eq!(AttemptId::parse_branch(bad), None, "{bad}");
        }
    }

    #[test]
    fn every_key_gets_a_hash_suffix_and_one_safe_segment() {
        for key in [
            "c1",
            "card-1",
            "A_b.c",
            "x.y-z",
            "",
            "a/b",
            "../x",
            "..",
            "-rf",
            "a b",
            ".hidden",
            "x..y",
            "a.lock",
            "tr.",
            "/abs",
            "e\u{301}",
            "a\nb",
            "x".repeat(200).as_str(),
        ] {
            let s = safe_segment(key);
            let (head, h) = s.rsplit_once('-').unwrap();
            assert_eq!(h.len(), HASH_LEN, "{s}");
            assert!(h.chars().all(|c| c.is_ascii_hexdigit()), "{s}");
            assert!(!head.is_empty(), "{s}");
            assert!(
                s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-')),
                "{key:?} -> {s}"
            );
            assert!(!s.contains("..") && !s.starts_with(['-', '.']), "{s}");
            assert!(!s.ends_with('.') && !s.ends_with(".lock"), "{s}");
            assert!(s.len() <= SEGMENT_MAX + HASH_LEN + 1, "{s}");
            assert_ne!(s, key, "{key:?} must always be suffixed");
        }
    }

    #[test]
    fn a_key_equal_to_anothers_rewritten_form_does_not_collide() {
        let rewritten = safe_segment("a/b");
        // The literal key that equals the rewritten form of "a/b".
        assert_ne!(safe_segment(&rewritten), rewritten);
        assert_ne!(
            AttemptId::new("a/b", 1).branch(),
            AttemptId::new(rewritten.clone(), 1).branch()
        );
        assert_ne!(
            AttemptId::new("a/b", 1).dir_name(),
            AttemptId::new(rewritten, 1).dir_name()
        );
    }

    #[test]
    fn distinct_keys_never_share_a_segment() {
        let keys = ["a/b", "a_b", "a b", "a\\b", "../a", "..a", "a/b/"];
        let segs: std::collections::HashSet<String> =
            keys.iter().map(|k| safe_segment(k)).collect();
        assert_eq!(segs.len(), keys.len(), "{segs:?}");
        let (x, y) = (AttemptId::new("a/b", 1), AttemptId::new("a_b", 1));
        assert_ne!(x.branch(), y.branch());
        assert_ne!(x.dir_name(), y.dir_name());
    }

    #[test]
    fn traversal_card_stays_inside_workroot_and_attempt_namespace() {
        let (repo_dir, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("../../evil", 1);
        assert!(id.branch().starts_with("attempt/") && !id.branch().contains(".."));
        let dest = root.path().join(id.dir_name());
        assert_eq!(dest.parent().unwrap(), root.path());
        let w = create(repo_dir.path(), &base, &dest, &id).unwrap();
        assert_eq!(
            git(&w.path, &["branch", "--show-current"]).unwrap(),
            id.branch()
        );
        // The branch parses back to the same segment, so the janitor and the
        // probe address the same worktree.
        let back = AttemptId::parse_branch(&id.branch()).unwrap();
        assert_eq!(back.branch(), id.branch());
        assert_eq!(back.dir_name(), id.dir_name());
    }

    #[test]
    fn create_list_remove() {
        let (repo_dir, base) = repo();
        let wt_root = tempfile::tempdir().unwrap();
        let id1 = AttemptId::new("c1", 1);
        let id2 = AttemptId::new("c1", 2);
        let w1 = create(repo_dir.path(), &base, &wt_root.path().join("w1"), &id1).unwrap();
        create(repo_dir.path(), &base, &wt_root.path().join("w2"), &id2).unwrap();
        assert_eq!(w1.head, base);
        assert_eq!(
            git(&w1.path, &["branch", "--show-current"]).unwrap(),
            id1.branch()
        );

        let mut listed = list(repo_dir.path()).unwrap();
        listed.sort_by_key(|w| w.id.clone());
        assert_eq!(listed.len(), 2, "main worktree is not an attempt");
        assert_eq!(listed[0].id, id1);
        assert_eq!(listed[1].id, id2);

        write(&w1.path, "f.txt", "x");
        commit_all(&w1.path, "work");
        remove(repo_dir.path(), &w1.path).unwrap();
        assert!(!w1.path.exists());
        assert_eq!(list(repo_dir.path()).unwrap().len(), 1);
        // branch survives removal
        let br = git(repo_dir.path(), &["branch", "--list", &id1.branch()]).unwrap();
        assert!(br.contains(&id1.branch()));
    }

    #[test]
    fn create_twice_same_branch_fails() {
        let (repo_dir, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c", 1);
        create(repo_dir.path(), &base, &root.path().join("a"), &id).unwrap();
        assert!(create(repo_dir.path(), &base, &root.path().join("b"), &id).is_err());
    }

    #[test]
    fn option_like_base_is_refused() {
        let (repo_dir, _) = repo();
        let root = tempfile::tempdir().unwrap();
        // `-f` would otherwise be parsed as `git worktree add --force` and the
        // worktree silently start from the main checkout's HEAD instead of the base.
        let dest = root.path().join("w");
        let id = AttemptId::new("c", 1);
        assert!(create(repo_dir.path(), "-f", &dest, &id).is_err());
        assert!(!dest.exists());
        assert!(git(repo_dir.path(), &["branch", "--list", &id.branch()])
            .unwrap()
            .is_empty());
    }
}
