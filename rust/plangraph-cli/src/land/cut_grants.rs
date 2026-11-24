//! The pre-cut grant record of `land-train cut` and the one authorization of a cut commit's message.
//! The record is a JSON array of `{d_id, prefix, head}` (the `go_verify` grant): one row per `Gate-Edit`
//! D-id, bound to the full id of the source commit and a path prefix.
use super::git;
use super::go_verify::Grant;
use std::io::Write;
use std::path::Path;
use std::process::Stdio;

/// The path prefixes the record gives `d_id` for exactly the source head `head`.
pub(super) fn prefixes<'a>(grants: &'a [Grant], d_id: &str, head: &str) -> Vec<&'a str> {
    grants
        .iter()
        .filter(|g| g.d_id == d_id && g.head == head)
        .map(|g| g.prefix.as_str())
        .collect()
}

/// Reads the record. It is operator authority, so only a protected file is accepted: a regular file (not a
/// link) owned by the caller that no other user can write.
pub(super) fn load_grants(path: &Path) -> Result<Vec<Grant>, String> {
    let name = path.display();
    let meta = std::fs::symlink_metadata(path).map_err(|e| format!("grants {name}: {e}"))?;
    if !meta.is_file() {
        return Err(format!("grants {name}: not a regular file"));
    }
    protected(&meta).map_err(|e| format!("grants {name}: {e}"))?;
    serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("grants {name}: {e}"))?)
        .map_err(|e| format!("grants {name}: {e}"))
}

#[cfg(unix)]
fn protected(meta: &std::fs::Metadata) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    // SAFETY: geteuid is a read-only identity query.
    if meta.uid() != unsafe { libc::geteuid() } {
        return Err("not owned by the caller".into());
    }
    if meta.mode() & 0o022 != 0 {
        return Err("writable by group or others".into());
    }
    Ok(())
}

#[cfg(not(unix))]
fn protected(_: &std::fs::Metadata) -> Result<(), String> {
    Err("the protected-file check needs a unix host".into())
}

/// The last `Picked-From:` value of `message`: the source this cut commit was made from.
fn picked_from(message: &str) -> Option<&str> {
    message
        .lines()
        .filter_map(|l| l.trim().strip_prefix("Picked-From:"))
        .map(str::trim)
        .next_back()
}

/// The stable patch id of `commit`'s change; `None` for an empty change.
fn patch_id(repo: &Path, commit: &str) -> Result<Option<String>, String> {
    let diff = git::bytes(
        repo,
        &[
            "diff-tree",
            "-p",
            "--root",
            "--no-commit-id",
            "--binary",
            commit,
        ],
    )?;
    let mut child = git::command(repo, None)
        .args(["patch-id", "--stable"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    child
        .stdin
        .take()
        .ok_or("patch-id has no stdin")?
        .write_all(&diff)
        .map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "git patch-id failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout)
        .split_whitespace()
        .next()
        .map(str::to_owned))
}

/// The one authorization of a message, for the cutter before and after the pick and for `check`. Every
/// `Gate-Edit:` line, wherever it stands (a trailer parse can miss one that cutting then exposes), needs:
/// - a single-parent commit (a merge's edits cannot be told from its parents');
/// - a grant row for its D-id and the source head (`head`, or the message's last `Picked-From:`);
/// - rows whose prefixes cover every path the commit changes (read NUL-delimited, so no path is skipped);
///   the pinned gate decides which paths are gate files, a second copy of its list is not kept here;
/// - when the commit is not the source itself, the source commit in the repository and the same patch id,
///   so a `Picked-From:` line cannot borrow a grant for another edit.
pub(super) fn authorize(
    repo: &Path,
    commit: &str,
    message: &str,
    head: Option<&str>,
    grants: &[Grant],
) -> Result<(), String> {
    let head = head.or_else(|| picked_from(message));
    let mut verified = false;
    for line in message.lines() {
        let Some((key, value)) = line.trim().split_once(':') else {
            continue;
        };
        if !key.eq_ignore_ascii_case("gate-edit") {
            continue;
        }
        let d = value.trim();
        let denied = |why: &str| {
            format!("cut: {commit} carries Gate-Edit {d}, which the GO row does not grant ({why})")
        };
        let head = head.ok_or_else(|| denied("no Picked-From names its source"))?;
        let prefixes = prefixes(grants, d, head);
        if prefixes.is_empty() {
            return Err(denied(&format!("no grant for source {head}")));
        }
        if verified {
            continue;
        }
        let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", commit])?;
        if parents.split_whitespace().count() != 2 {
            return Err(denied("not a single-parent commit"));
        }
        let raw = git::bytes(
            repo,
            &[
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
            ],
        )?;
        for path in raw.split(|b| *b == 0).filter(|p| !p.is_empty()) {
            let path = String::from_utf8_lossy(path);
            if !prefixes
                .iter()
                .any(|p| *p == path || (p.ends_with('/') && path.starts_with(p)))
            {
                return Err(denied(&format!("no grant covers {path}")));
            }
        }
        if commit != head {
            let known = git::run(
                repo,
                None,
                &["cat-file", "-e", &format!("{head}^{{commit}}")],
            )?;
            if !known.status.success() {
                return Err(denied(&format!("source {head} is not in this repository")));
            }
            if patch_id(repo, commit)? != patch_id(repo, head)? {
                return Err(denied(&format!("the change differs from source {head}")));
            }
        }
        verified = true;
    }
    Ok(())
}
