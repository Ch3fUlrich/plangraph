//! The pre-cut grant record of `land-train cut` and the one authorization of a cut commit's message.
//! The record is a JSON array of `{d_id, prefix, head}` (the `go_verify` grant): one row per `Gate-Edit`
//! D-id, bound to the full id of the source commit and a path prefix.
use super::git;
use super::go_verify::Grant;
use std::path::Path;

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

/// Whether `commit` is exactly the change of `source` replayed onto `commit`'s parent: the three-way merge of
/// `source` onto `commit^` with `source^` as base gives `commit`'s own tree, whitespace included.
fn replays(repo: &Path, commit: &str, source: &str) -> Result<bool, String> {
    let single = |rev: &str| -> Result<(), String> {
        let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", rev])?;
        if parents.split_whitespace().count() == 2 {
            Ok(())
        } else {
            Err(format!("{rev} is not a single-parent commit"))
        }
    };
    single(commit)?;
    single(source)?;
    let out = git::run(
        repo,
        None,
        &[
            "merge-tree",
            "--write-tree",
            "--merge-base",
            &format!("{source}^"),
            &format!("{commit}^"),
            source,
        ],
    )?;
    if !out.status.success() {
        // Exit 1 is a conflict: the source does not apply to the parent, so the commit is not its replay.
        return match out.status.code() {
            Some(1) => Ok(false),
            _ => Err(format!(
                "git merge-tree failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            )),
        };
    }
    let replayed = String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()
        .unwrap_or_default()
        .to_owned();
    Ok(replayed == git::out(repo, &["rev-parse", &format!("{commit}^{{tree}}")])?)
}

/// The one authorization of a message, for the cutter before and after the pick and for `check`. Every
/// `Gate-Edit:` line, wherever it stands (a trailer parse can miss one that cutting then exposes), needs:
/// - a single-parent commit (a merge's edits cannot be told from its parents');
/// - for each of its D-ids, grant rows for that D-id and the source head (`head`, or the message's last
///   `Picked-From:`) whose prefixes cover every path the commit changes (raw NUL-delimited bytes, so no path
///   is skipped or merged with another); the pinned gate decides which paths are gate files;
/// - when the commit is not the source itself, the source commit in the repository and the commit being its
///   exact replay on the commit's parent, so a `Picked-From:` line cannot borrow a grant for another edit.
pub(super) fn authorize(
    repo: &Path,
    commit: &str,
    message: &str,
    head: Option<&str>,
    grants: &[Grant],
) -> Result<(), String> {
    let head = head.or_else(|| picked_from(message));
    let mut ids: Vec<&str> = Vec::new();
    for line in message.lines() {
        if let Some((key, value)) = line.trim().split_once(':') {
            if key.eq_ignore_ascii_case("gate-edit") && !ids.contains(&value.trim()) {
                ids.push(value.trim());
            }
        }
    }
    if ids.is_empty() {
        return Ok(());
    }
    let denied = |d: &str, why: &str| {
        format!("cut: {commit} carries Gate-Edit {d}, which the GO row does not grant ({why})")
    };
    let head = head.ok_or_else(|| denied(ids[0], "no Picked-From names its source"))?;
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
    let paths: Vec<&[u8]> = raw.split(|b| *b == 0).filter(|p| !p.is_empty()).collect();
    for d in &ids {
        let rows = prefixes(grants, d, head);
        if rows.is_empty() {
            return Err(denied(d, &format!("no grant for source {head}")));
        }
        for path in &paths {
            if !rows.iter().any(|p| {
                let p = p.as_bytes();
                *path == p || (p.ends_with(b"/") && path.starts_with(p))
            }) {
                let shown = String::from_utf8_lossy(path);
                return Err(denied(d, &format!("no grant covers {shown}")));
            }
        }
    }
    let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", commit])?;
    if parents.split_whitespace().count() != 2 {
        return Err(denied(ids[0], "not a single-parent commit"));
    }
    if commit != head {
        let known = git::run(
            repo,
            None,
            &["cat-file", "-e", &format!("{head}^{{commit}}")],
        )?;
        if !known.status.success() {
            return Err(denied(
                ids[0],
                &format!("source {head} is not in this repository"),
            ));
        }
        if !replays(repo, commit, head).map_err(|e| denied(ids[0], &e))? {
            return Err(denied(
                ids[0],
                &format!("the change is not the exact replay of source {head}"),
            ));
        }
    }
    Ok(())
}
