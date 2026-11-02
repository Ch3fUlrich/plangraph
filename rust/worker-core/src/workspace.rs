//! Workspace of a card: the checkout a placement requests (plan rev 6.4, D6).
//! Pure types and validation: no git calls, no daemon, no network. The requested
//! revision is `Workspace::revision_sha`; the produced one is `WorkspaceRecord::head_at_end`.

use serde::{Deserialize, Serialize};

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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub revision_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub branch: Option<String>,
}

/// What a workspace run produced; shas are checked by `validate`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceRecord {
    pub repo_id: String,
    pub toplevel: String,
    #[serde(skip_serializing_if = "Option::is_none")]
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
    #[error("branch {0:?} must start with `lane/` and never be a default branch name")]
    BranchNotLane(String),
    #[error("branch {0:?} must not contain `..`")]
    BranchDotDot(String),
    #[error("branch {0:?} must not contain `//`")]
    BranchDoubleSlash(String),
    #[error("branch {0:?} must not contain `@{{`")]
    BranchAtBrace(String),
    #[error("branch {0:?} must not contain control characters")]
    BranchControl(String),
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
    let long = b.chars().count() > 200;
    let ctrl = name.chars().any(char::is_control);
    let weird = name.chars().any(|c| "~^:?*[\\".contains(c));
    let space = name.contains(' ');
    let tail = name.ends_with('/') || name.ends_with('.');
    let dot = name.split('/').any(|c| c.starts_with('.'));
    let lock = name.split('/').any(|c| c.ends_with(".lock"));
    let checks = [
        (long, e(E::BranchTooLong)),
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

impl Workspace {
    /// Parse a workspace from JSON and validate every rule (D6).
    pub fn from_json_str(s: &str) -> Result<Self, WorkspaceError> {
        let w: Self = serde_json::from_str(s)?;
        w.validate()?;
        Ok(w)
    }

    /// Check repo_id, sha shapes, per-mode presence and the branch rules.
    pub fn validate(&self) -> Result<(), WorkspaceError> {
        let id = &self.repo_id;
        let id_ok = !id.is_empty()
            && id.len() <= 64
            && id.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
            && id
                .chars()
                .all(|c| matches!(c, 'a'..='z' | '0'..='9' | '.' | '_' | '-'));
        if !id_ok {
            return Err(WorkspaceError::RepoId(id.clone()));
        }
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
    /// Every sha on a record is exactly 40 lowercase hex characters.
    pub fn validate(&self) -> Result<(), WorkspaceError> {
        check_sha("base_sha", &self.base_sha)?;
        check_sha("head_at_start", &self.head_at_start)?;
        check_sha("head_at_end", &self.head_at_end)?;
        Ok(())
    }

    /// What the writer produced: `{repo_id, base_sha, head_at_end}`.
    pub fn writer_result_diff(&self) -> DiffRef {
        DiffRef {
            repo_id: self.repo_id.clone(),
            base_sha: self.base_sha.clone(),
            head_sha: self.head_at_end.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const W: &str = include_str!("../../../docs/schemas/examples/workspace.writer.json");
    const S: &str = include_str!("../../../docs/schemas/examples/workspace.seat.json");
    const R: &str = include_str!("../../../docs/schemas/examples/workspace.read.json");
    const N: &str = include_str!("../../../docs/schemas/examples/workspace.none.json");
    const SCHEMA: &str = include_str!("../../../docs/schemas/workspace.schema.json");

    #[test]
    fn examples_parse_and_validate() {
        for (mode, src) in [("writer", W), ("seat", S), ("read", R), ("none", N)] {
            let w = Workspace::from_json_str(src).expect(mode);
            let name = format!("{:?}", w.mode).to_lowercase();
            assert_eq!(name, mode);
        }
    }

    fn fill(src: &str) -> String {
        let long = format!("lane/{}", "a".repeat(196));
        src.replace("@A", A).replace("@B", B).replace("@L", &long)
    }

    /// (rule the error must name)|(json); one refusal per rule and mode.
    const REFUSE: &[&str] = &[
        r#"base_sha is required|{"repo_id":"p","mode":"writer","revision_sha":"@A","branch":"lane/x"}"#,
        r#"revision_sha is required|{"repo_id":"p","mode":"writer","base_sha":"@A","branch":"lane/x"}"#,
        r#"branch is required|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A"}"#,
        r#"must equal|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@B","branch":"lane/x"}"#,
        r#"`lane/`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"main"}"#,
        r#"`..`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a..b"}"#,
        r#"`//`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a//b"}"#,
        r#"`@{`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a@{b"}"#,
        "control characters|{\"repo_id\":\"p\",\"mode\":\"writer\",\"base_sha\":\"@A\",\"revision_sha\":\"@A\",\"branch\":\"lane/a\\tb\"}",
        r#"spaces|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a b"}"#,
        r#"must not contain any of|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a~b"}"#,
        r#"start or end|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/.x"}"#,
        r#".lock|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x.lock"}"#,
        r#"200|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"@L"}"#,
        r#"40 lowercase hex|{"repo_id":"p","mode":"writer","base_sha":"deadbeef","revision_sha":"@A","branch":"lane/x"}"#,
        r#"40 lowercase hex|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","branch":"lane/x"}"#,
        r#"40 lowercase hex|{"repo_id":"p","mode":"writer","base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","revision_sha":"@A","branch":"lane/x"}"#,
        r#"repo_id|{"repo_id":"Repo","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x"}"#,
        r#"workspace json|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x","extra":1}"#,
        r#"base_sha is required|{"repo_id":"p","mode":"seat","revision_sha":"@A"}"#,
        r#"revision_sha is required|{"repo_id":"p","mode":"seat","base_sha":"@A"}"#,
        r#"must be absent|{"repo_id":"p","mode":"seat","base_sha":"@A","revision_sha":"@A","branch":"lane/x"}"#,
        r#"40 lowercase hex|{"repo_id":"p","mode":"seat","base_sha":"@A","revision_sha":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}"#,
        r#"repo_id|{"repo_id":"r!","mode":"seat","base_sha":"@A","revision_sha":"@A"}"#,
        r#"workspace json|{"repo_id":"p","mode":"seat","base_sha":"@A","revision_sha":"@A","extra":1}"#,
        r#"revision_sha is required|{"repo_id":"p","mode":"read"}"#,
        r#"must be absent|{"repo_id":"p","mode":"read","revision_sha":"@A","branch":"lane/x"}"#,
        r#"40 lowercase hex|{"repo_id":"p","mode":"read","base_sha":"deadbeef","revision_sha":"@A"}"#,
        r#"workspace json|{"repo_id":"p","mode":"read","revision_sha":"@A","extra":1}"#,
        r#"base_sha must be absent|{"repo_id":"p","mode":"none","base_sha":"@A"}"#,
        r#"revision_sha must be absent|{"repo_id":"p","mode":"none","revision_sha":"@A"}"#,
        r#"branch must be absent|{"repo_id":"p","mode":"none","branch":"lane/x"}"#,
        r#"missing field|{"mode":"none"}"#,
        r#"workspace json|{"repo_id":"p","mode":"none","extra":1}"#,
    ];

    #[test]
    fn refusals_name_the_field_and_rule() {
        for case in REFUSE {
            let (rule, src) = case.split_once('|').unwrap();
            let e = Workspace::from_json_str(&fill(src)).unwrap_err();
            assert!(e.to_string().contains(rule), "{case}: {e}");
        }
    }

    #[test]
    fn seat_input_diff_maps_revision_to_head() {
        let seat = Workspace::from_json_str(S).unwrap();
        let d = seat.seat_input_diff().unwrap();
        assert_eq!(d.base_sha, seat.base_sha.unwrap());
        assert_eq!(d.head_sha, seat.revision_sha.unwrap());
        assert_eq!(d.repo_id, "plangraph");
        let writer = Workspace::from_json_str(W).unwrap();
        assert!(matches!(
            writer.seat_input_diff(),
            Err(WorkspaceError::NotSeat(Mode::Writer))
        ));
    }

    #[test]
    fn record_validates_shas_and_maps_head_at_end() {
        let good = format!("{{\"repo_id\":\"p\",\"toplevel\":\"t\",\"base_sha\":\"{A}\",\"head_at_start\":\"{A}\",\"head_at_end\":\"{B}\"}}");
        let mut rec: WorkspaceRecord = serde_json::from_str(&good).unwrap();
        assert!(rec.validate().is_ok());
        let d = rec.writer_result_diff();
        assert_eq!((d.base_sha.as_str(), d.head_sha.as_str()), (A, B));
        rec.head_at_end = "short".into();
        assert!(matches!(rec.validate(), Err(WorkspaceError::Sha(f, _)) if f == "head_at_end"));
    }

    #[test]
    fn schema_properties_match_serde_fields() {
        let schema: serde_json::Value = serde_json::from_str(SCHEMA).unwrap();
        let mut fields = BTreeSet::new();
        for src in [W, S, R, N] {
            let v = serde_json::to_value(Workspace::from_json_str(src).unwrap()).unwrap();
            fields.extend(v.as_object().unwrap().keys().cloned());
        }
        let props = schema["properties"].as_object().unwrap();
        assert_eq!(props.keys().cloned().collect::<BTreeSet<_>>(), fields);
    }
}
