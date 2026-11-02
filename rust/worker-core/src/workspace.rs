//! Workspace of a card: the checkout a placement requests (plan rev 6.4, D6).
//! Pure types and validation: no git calls, no daemon, no network. The requested
//! revision is `Workspace::revision_sha`; the produced one is `WorkspaceRecord::head_at_end`.

use serde::{Deserialize, Serialize};

// Missing optional fields default to None; present fields must be strings, not null.
fn present_string<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<String>, D::Error> {
    String::deserialize(d).map(Some)
}

/// What a placement asks the worker to do with `repo_id`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Writer,
    Seat,
    Read,
    None,
}

/// A card carries no paths: only the repo, the mode and the revisions.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workspace {
    pub repo_id: String,
    pub mode: Mode,
    #[serde(
        default,
        deserialize_with = "present_string",
        skip_serializing_if = "Option::is_none"
    )]
    pub base_sha: Option<String>,
    #[serde(
        default,
        deserialize_with = "present_string",
        skip_serializing_if = "Option::is_none"
    )]
    pub revision_sha: Option<String>,
    #[serde(
        default,
        deserialize_with = "present_string",
        skip_serializing_if = "Option::is_none"
    )]
    pub branch: Option<String>,
}

/// What a workspace run produced; repo, branch and shas are checked by `validate`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceRecord {
    pub repo_id: String,
    pub toplevel: String,
    #[serde(
        default,
        deserialize_with = "present_string",
        skip_serializing_if = "Option::is_none"
    )]
    pub branch: Option<String>,
    pub base_sha: String,
    pub head_at_start: String,
    pub head_at_end: String,
}

/// A `base_sha..head_sha` comparison in one repo.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiffRef {
    pub repo_id: String,
    pub base_sha: String,
    pub head_sha: String,
}

/// Every refusal names the field and the rule it violates.
#[derive(Debug, thiserror::Error)]
pub enum WorkspaceError {
    #[error("repo_id {0:?} must match ^[a-z0-9][a-z0-9._-]{{0,63}}$")]
    RepoId(String),
    #[error("{0} must be exactly 40 lowercase hex characters, got {1:?}")]
    Sha(&'static str, String),
    #[error("{0} is required in mode {1:?}")]
    Missing(&'static str, Mode),
    #[error("{0} must be absent in mode {1:?}")]
    Absent(&'static str, Mode),
    #[error("writer base_sha {0:?} must equal revision_sha {1:?}")]
    WriterShasDiffer(String, String),
    #[error("writer branch {0:?} must start with `lane/`")]
    BranchNotLane(String),
    #[error("branch {0:?} must not contain `..`")]
    BranchDotDot(String),
    #[error("branch {0:?} must not contain `//`")]
    BranchDoubleSlash(String),
    #[error("branch {0:?} must not contain `@{{`")]
    BranchAtBrace(String),
    #[error("branch {0:?} must not contain control characters")]
    BranchControl(String),
    #[error("branch {0:?} must contain only ASCII characters")]
    BranchNonAscii(String),
    #[error("branch {0:?} must not contain spaces")]
    BranchSpace(String),
    #[error("branch {0:?} must not contain any of `~^:?*[\\`")]
    BranchBadChar(String),
    #[error("branch {0:?} must not start or end with `/` or `.`")]
    BranchEdge(String),
    #[error("branch {0:?} must not have a component ending in `.lock`")]
    BranchLock(String),
    #[error("branch {0:?} must be at most 200 characters")]
    BranchTooLong(String),
    #[error("seat_input_diff applies only to mode seat, got {0:?}")]
    NotSeat(Mode),
    #[error("workspace json: {0}")]
    Json(#[from] serde_json::Error),
}

fn check_sha(field: &'static str, got: &str) -> Result<(), WorkspaceError> {
    let hex = |b: u8| b.is_ascii_digit() || (b'a'..=b'f').contains(&b);
    if got.len() == 40 && got.bytes().all(hex) {
        Ok(())
    } else {
        Err(WorkspaceError::Sha(field, got.to_string()))
    }
}

fn check_branch(b: &str) -> Result<(), WorkspaceError> {
    use WorkspaceError as E;
    let name = b
        .strip_prefix("lane/")
        .ok_or_else(|| E::BranchNotLane(b.to_string()))?;
    let e = |f: fn(String) -> E| f(b.to_string());
    let long = b.len() > 200;
    let ctrl = name.chars().any(char::is_control);
    let weird = name.chars().any(|c| "~^:?*[\\".contains(c));
    let space = name.contains(' ');
    let tail = name.ends_with('/') || name.ends_with('.');
    let dot = name.split('/').any(|c| c.starts_with('.'));
    let lock = name.split('/').any(|c| c.ends_with(".lock"));
    let checks = [
        (long, e(E::BranchTooLong)),
        (!name.is_ascii(), e(E::BranchNonAscii)),
        (b.contains(".."), e(E::BranchDotDot)),
        (b.contains("//"), e(E::BranchDoubleSlash)),
        (b.contains("@{"), e(E::BranchAtBrace)),
        (ctrl, e(E::BranchControl)),
        (space, e(E::BranchSpace)),
        (weird, e(E::BranchBadChar)),
        (name.is_empty() || tail || dot, e(E::BranchEdge)),
        (lock, e(E::BranchLock)),
    ];
    match checks.into_iter().find(|(bad, _)| *bad) {
        Some((_, err)) => Err(err),
        None => Ok(()),
    }
}

fn check_repo_id(id: &str) -> Result<(), WorkspaceError> {
    let valid = !id.is_empty()
        && id.len() <= 64
        && id.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
        && id
            .chars()
            .all(|c| matches!(c, 'a'..='z' | '0'..='9' | '.' | '_' | '-'));
    if valid {
        Ok(())
    } else {
        Err(WorkspaceError::RepoId(id.into()))
    }
}

impl Workspace {
    /// Parse a workspace from JSON and validate every rule (D6).
    pub fn from_json_str(s: &str) -> Result<Self, WorkspaceError> {
        let w: Self = serde_json::from_str(s)?;
        w.validate()?;
        Ok(w)
    }

    /// Check repo_id, sha shapes, per-mode presence and the branch rules.
    pub fn validate(&self) -> Result<(), WorkspaceError> {
        check_repo_id(&self.repo_id)?;
        if let Some(sha) = &self.base_sha {
            check_sha("base_sha", sha)?;
        }
        if let Some(sha) = &self.revision_sha {
            check_sha("revision_sha", sha)?;
        }
        let (need_base, need_rev, need_branch) = match self.mode {
            Mode::Writer => (true, true, true),
            Mode::Seat => (true, true, false),
            Mode::Read => (false, true, false),
            Mode::None => (false, false, false),
        };
        for (field, present, required) in [
            ("base_sha", self.base_sha.is_some(), need_base),
            ("revision_sha", self.revision_sha.is_some(), need_rev),
            ("branch", self.branch.is_some(), need_branch),
        ] {
            if required && !present {
                return Err(WorkspaceError::Missing(field, self.mode));
            }
            if !required && present && !(field == "base_sha" && self.mode == Mode::Read) {
                return Err(WorkspaceError::Absent(field, self.mode));
            }
        }
        if self.mode == Mode::Writer && self.base_sha != self.revision_sha {
            let b = self.base_sha.clone().unwrap_or_default();
            let r = self.revision_sha.clone().unwrap_or_default();
            return Err(WorkspaceError::WriterShasDiffer(b, r));
        }
        if let Some(branch) = &self.branch {
            check_branch(branch)?;
        }
        Ok(())
    }

    /// The comparison a seat reviews: `{repo_id, base_sha, revision_sha}`.
    pub fn seat_input_diff(&self) -> Result<DiffRef, WorkspaceError> {
        self.validate()?;
        if self.mode != Mode::Seat {
            return Err(WorkspaceError::NotSeat(self.mode));
        }
        let m = |f| WorkspaceError::Missing(f, self.mode);
        Ok(DiffRef {
            repo_id: self.repo_id.clone(),
            base_sha: self.base_sha.clone().ok_or_else(|| m("base_sha"))?,
            head_sha: self.revision_sha.clone().ok_or_else(|| m("revision_sha"))?,
        })
    }
}

impl WorkspaceRecord {
    /// Check the same repo and branch rules as a workspace, plus every sha.
    pub fn validate(&self) -> Result<(), WorkspaceError> {
        check_repo_id(&self.repo_id)?;
        if let Some(branch) = &self.branch {
            check_branch(branch)?;
        }
        check_sha("base_sha", &self.base_sha)?;
        check_sha("head_at_start", &self.head_at_start)?;
        check_sha("head_at_end", &self.head_at_end)?;
        Ok(())
    }

    /// What the writer produced: `{repo_id, base_sha, head_at_end}`.
    pub fn writer_result_diff(&self) -> Result<DiffRef, WorkspaceError> {
        self.validate()?;
        Ok(DiffRef {
            repo_id: self.repo_id.clone(),
            base_sha: self.base_sha.clone(),
            head_sha: self.head_at_end.clone(),
        })
    }
}

#[cfg(test)]
mod tests;
