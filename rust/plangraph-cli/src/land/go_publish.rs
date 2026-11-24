//! Publishes a computed GO as one row of the protected decisions branch and reads it back
//! (LE-LAND-GO-PUBLISH-BOUND-ROW).
//!
//! The decisions branch is protected: nobody pushes to it. A row reaches it through a row-only pull
//! request that a trusted principal merges (`RowPr`). This module reads the tip, allocates the next
//! D-id, appends exactly one row, submits it, and after the merge reads `origin`'s branch again: the row
//! must be there once, byte for byte, with every earlier line kept, and the keeper's canonical reader
//! (`DecisionsAdapter` + `_go_binding`) must read it as a GO bound to the PR and head. A moved branch is
//! a collision: the whole read, staleness check and allocation run again on the new tip, so another
//! decision is never overwritten and a GO that went stale while waiting is not published.
use super::git;
use std::path::Path;
use std::process::Command;

/// The GO to publish: the decision cell is derived from the PR and the exact head it binds.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GoRow {
    pub project: String,
    pub pr: u64,
    pub head: String,
    pub by: String,
    pub why: String,
}

impl GoRow {
    /// The row as the decisions log holds it: `| D-N | project | GO #PR GO-HEAD head | by | why |`.
    pub fn line(&self, d_id: &str) -> String {
        format!(
            "| {d_id} | {} | GO #{} GO-HEAD {} | {} | {} |",
            self.project, self.pr, self.head, self.by, self.why
        )
    }
}

/// Why a submission did not become a row of the branch.
#[derive(Debug, PartialEq, Eq)]
pub enum Submit {
    /// The protected branch is no longer at the tip the row was built on: read again.
    Moved,
    Refused(String),
}

/// The row-only pull request and its trusted principal; the protected branch is read through `read`.
pub trait Decisions {
    /// A fresh read of the protected branch: its tip commit and the text of the decisions file at it.
    fn read(&self) -> Result<(String, String), String>;
    /// Submits `text` (the tip text plus one row) built on `tip` and has it merged into the branch.
    fn submit(&self, tip: &str, d_id: &str, text: &str) -> Result<(), Submit>;
}

/// The keeper's canonical reader of a decisions row: the `(pr, head)` a GO row binds, if any.
pub trait Reader {
    fn bound(&self, text: &str, d_id: &str, project: &str)
        -> Result<Option<(u64, String)>, String>;
}

#[derive(Debug, PartialEq, Eq)]
pub struct Published {
    pub d_id: String,
    pub tip: String,
    pub attempts: usize,
}

/// The next free D-id of `text`: one above the highest `| D-N |` row outside fenced code.
pub fn next_id(text: &str) -> String {
    let mut fenced = false;
    let mut top = 0u64;
    for line in text.lines() {
        let t = line.trim_start();
        if t.starts_with("```") || t.starts_with("~~~") {
            fenced = !fenced;
        } else if !fenced {
            let id = line
                .strip_prefix("| D-")
                .and_then(|rest| rest.split_once(" |"))
                .and_then(|(n, _)| {
                    (!n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())).then_some(n)
                })
                .and_then(|n| n.parse::<u64>().ok());
            top = top.max(id.unwrap_or(0));
        }
    }
    format!("D-{}", top + 1)
}

fn appended(text: &str, row: &str) -> String {
    let mut out = text.to_owned();
    if !out.is_empty() && !out.ends_with('\n') {
        out.push('\n');
    }
    out.push_str(row);
    out.push('\n');
    out
}

/// The read-back proof: the branch holds the row once, exactly, and kept every earlier line in order.
fn check_read_back(
    before: &str,
    after: &str,
    row: &str,
    d_id: &str,
    go: &GoRow,
    reader: &dyn Reader,
) -> Result<(), String> {
    let marker = format!("| {d_id} |");
    let own: Vec<&str> = after.lines().filter(|l| l.starts_with(&marker)).collect();
    match own.as_slice() {
        [] => return Err(format!("read-back: {d_id} is not on the protected branch")),
        [one] if *one == row => {}
        [one] => {
            return Err(format!(
                "read-back: {d_id} on the protected branch is another decision: {one}"
            ))
        }
        _ => return Err(format!("read-back: {d_id} appears more than once")),
    }
    let mut later = after.lines();
    for line in before.lines() {
        if !later.any(|l| l == line) {
            return Err(format!(
                "read-back: an earlier decision line changed: {line}"
            ));
        }
    }
    match reader.bound(after, d_id, &go.project)? {
        Some((pr, head)) if pr == go.pr && head == go.head => Ok(()),
        other => Err(format!(
            "read-back: the keeper's reader binds {other:?}, not GO #{} {}",
            go.pr, go.head
        )),
    }
}

/// Publishes `go` and returns after the read-back proof. `fresh` runs on every tip before a row is built
/// (the GO's evidence must still hold there); `max_attempts` bounds the collision retries.
pub fn publish(
    branch: &dyn Decisions,
    reader: &dyn Reader,
    go: &GoRow,
    fresh: &dyn Fn(&str) -> Result<(), String>,
    max_attempts: usize,
) -> Result<Published, String> {
    for attempt in 1..=max_attempts {
        let (tip, text) = branch.read()?;
        fresh(&tip).map_err(|e| format!("stale: {e}"))?;
        let d_id = next_id(&text);
        let row = go.line(&d_id);
        match branch.submit(&tip, &d_id, &appended(&text, &row)) {
            Ok(()) => {
                let (after_tip, after) = branch.read()?;
                check_read_back(&text, &after, &row, &d_id, go, reader)?;
                return Ok(Published {
                    d_id,
                    tip: after_tip,
                    attempts: attempt,
                });
            }
            Err(Submit::Moved) => continue,
            Err(Submit::Refused(why)) => return Err(format!("row refused: {why}")),
        }
    }
    Err(format!(
        "the decisions branch kept moving: {max_attempts} attempts used"
    ))
}

/// The merge step of the row-only pull request, run by the trusted principal.
pub trait RowPr {
    /// Merges the row branch `pr_ref` into the protected branch only while it still is at `tip`.
    fn merge(&self, pr_ref: &str, tip: &str) -> Result<(), Submit>;
}

/// The protected branch of a git remote: fetched fresh, never pushed to; rows go out on `go-row/*`
/// branches and the principal merges them.
pub struct GitDecisions<'a> {
    pub repo: &'a Path,
    pub remote: &'a str,
    pub branch: &'a str,
    /// The decisions file, relative to the repository root.
    pub path: &'a str,
    pub principal: &'a dyn RowPr,
}

impl GitDecisions<'_> {
    fn tracking(&self) -> String {
        format!("refs/land/decisions/{}", self.branch)
    }

    /// The commit of `tip` with the file replaced by `text`, built with a private index.
    fn row_commit(&self, tip: &str, d_id: &str, text: &str) -> Result<String, String> {
        let index = tempfile::tempdir().map_err(|e| e.to_string())?;
        let idx = index.path().join("index");
        let with_index = |args: &[&str]| -> Result<std::process::Output, String> {
            let mut c: Command = git::command(self.repo, None);
            c.env("GIT_INDEX_FILE", &idx).args(args);
            c.output().map_err(|e| e.to_string())
        };
        let blob_file = index.path().join("blob");
        std::fs::write(&blob_file, text).map_err(|e| e.to_string())?;
        let blob = git::out(
            self.repo,
            &["hash-object", "-w", &blob_file.to_string_lossy()],
        )?;
        let run = |args: &[&str]| -> Result<String, String> {
            let o = with_index(args)?;
            if o.status.success() {
                Ok(String::from_utf8_lossy(&o.stdout).trim().to_owned())
            } else {
                Err(format!(
                    "git {args:?}: {}",
                    String::from_utf8_lossy(&o.stderr).trim()
                ))
            }
        };
        run(&["read-tree", tip])?;
        run(&[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("100644,{blob},{}", self.path),
        ])?;
        let tree = run(&["write-tree"])?;
        git::out(
            self.repo,
            &[
                "commit-tree",
                &tree,
                "-p",
                tip,
                "-m",
                &format!("decisions: {d_id} GO row"),
            ],
        )
    }
}

impl Decisions for GitDecisions<'_> {
    fn read(&self) -> Result<(String, String), String> {
        let spec = format!("+refs/heads/{}:{}", self.branch, self.tracking());
        git::out(self.repo, &["fetch", "--quiet", self.remote, &spec])
            .map_err(|e| format!("the protected branch {} cannot be read: {e}", self.branch))?;
        let tip = git::out(self.repo, &["rev-parse", &self.tracking()])?;
        let bytes = git::bytes(self.repo, &["show", &format!("{tip}:{}", self.path)])
            .map_err(|e| format!("{} is not on the protected branch: {e}", self.path))?;
        let text = String::from_utf8(bytes).map_err(|_| format!("{} is not UTF-8", self.path))?;
        Ok((tip, text))
    }

    fn submit(&self, tip: &str, d_id: &str, text: &str) -> Result<(), Submit> {
        let refused = Submit::Refused;
        // Row-only: the text is the tip's text plus exactly one line, nothing else changes.
        let old = git::bytes(self.repo, &["show", &format!("{tip}:{}", self.path)])
            .map_err(Submit::Refused)?;
        let added = text.strip_prefix(String::from_utf8_lossy(&old).as_ref());
        if added.is_none_or(|a| a.lines().count() != 1 || !a.ends_with('\n')) {
            return Err(Submit::Refused("the change is not one appended row".into()));
        }
        let commit = self.row_commit(tip, d_id, text).map_err(refused)?;
        // The row branch is named by id and tip: a pending row of the same id and tip is a collision.
        let pr_ref = format!("refs/heads/go-row/{d_id}-{}", &tip[..12.min(tip.len())]);
        let pushed = git::run(
            self.repo,
            None,
            &[
                "push",
                "--quiet",
                self.remote,
                &format!("{commit}:{pr_ref}"),
            ],
        )
        .map_err(Submit::Refused)?;
        if !pushed.status.success() {
            return Err(Submit::Moved);
        }
        self.principal.merge(&pr_ref, tip)
    }
}

/// The reader of the keeper (`DecisionsAdapter` with `_go_binding`), run through `python`.
pub struct KeeperReader<'a> {
    pub python: &'a dyn Fn() -> Command,
}

impl Reader for KeeperReader<'_> {
    fn bound(
        &self,
        text: &str,
        d_id: &str,
        project: &str,
    ) -> Result<Option<(u64, String)>, String> {
        const READ: &str = "import sys\nfrom pathlib import Path\n\
            from plangraph.keeper.evidence_decisions import DecisionsAdapter\n\
            from plangraph.keeper.store_waiver import _go_binding, _go_statement\n\
            cell = DecisionsAdapter(Path(sys.argv[1])).text(sys.argv[2])\n\
            pr, head = _go_binding(_go_statement(cell), sys.argv[3])\n\
            print(pr if pr is not None else '', head or '')";
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let file = dir.path().join("DECISIONS.md");
        std::fs::write(&file, text).map_err(|e| e.to_string())?;
        let out = (self.python)()
            .args(["-c", READ])
            .arg(&file)
            .args([d_id, project])
            .output()
            .map_err(|e| format!("the keeper's GO reader is unavailable: {e}"))?;
        if !out.status.success() {
            return Err(format!(
                "the keeper's GO reader failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        let text = String::from_utf8_lossy(&out.stdout);
        Ok(match text.trim().split_once(' ') {
            Some((pr, head)) if !pr.is_empty() && !head.is_empty() => {
                pr.parse().ok().map(|n| (n, head.to_owned()))
            }
            _ => None,
        })
    }
}
