use super::{NavResult, RecentCommit};
use std::path::{Path, PathBuf};
use std::process::Command;

fn git(root: &Path, args: &[&str]) -> NavResult<String> {
    let output = Command::new("git")
        .current_dir(root)
        .env("GIT_LITERAL_PATHSPECS", "1")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .args(args)
        .output()?;
    if !output.status.success() {
        return Err(format!(
            "git {}: {}",
            args[0],
            String::from_utf8_lossy(&output.stderr).trim()
        )
        .into());
    }
    Ok(String::from_utf8(output.stdout)?)
}

pub(super) fn root(cwd: &Path) -> NavResult<PathBuf> {
    Ok(PathBuf::from(
        git(cwd, &["rev-parse", "--show-toplevel"])?.trim_end_matches('\n'),
    ))
}

pub(super) fn head(root: &Path) -> NavResult<String> {
    Ok(git(root, &["rev-parse", "HEAD"])?.trim().to_owned())
}

pub(super) fn default_index(root: &Path) -> NavResult<PathBuf> {
    Ok(root
        .join(git(root, &["rev-parse", "--git-path", "pg-ctx-nav.json"])?.trim_end_matches('\n')))
}

pub(super) fn files(root: &Path, commit: &str) -> NavResult<Vec<(String, gix::ObjectId)>> {
    git(root, &["ls-tree", "-rz", commit])?
        .split_terminator('\0')
        .map(|entry| {
            let (header, path) = entry.split_once('\t').ok_or("invalid git tree entry")?;
            let (mode, oid) = header.rsplit_once(' ').ok_or("invalid git tree object")?;
            Ok((matches!(mode, "100644 blob" | "100755 blob"))
                .then(|| gix::ObjectId::from_hex(oid.as_bytes()).map(|oid| (path.to_owned(), oid)))
                .transpose()?)
        })
        .filter_map(Result::transpose)
        .collect()
}

pub(super) fn changed(root: &Path, commit: &str, head: &str) -> NavResult<Vec<String>> {
    // Separate diffs preserve both a committed change and its uncommitted reversal.
    let committed = git(
        root,
        &[
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            commit,
            head,
            "--",
        ],
    )?;
    let staged = git(
        root,
        &[
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            head,
            "--",
        ],
    )?;
    let unstaged = git(root, &["diff", "--name-only", "--no-renames", "-z", "--"])?;
    let mut paths: Vec<_> = [&committed, &staged, &unstaged]
        .into_iter()
        .flat_map(|output| output.split_terminator('\0').map(str::to_owned))
        .collect();
    paths.sort_unstable();
    paths.dedup();
    Ok(paths)
}

pub(super) fn history(root: &Path, commit: &str, paths: &[&str]) -> NavResult<Vec<RecentCommit>> {
    if paths.is_empty() {
        return Ok(Vec::new());
    }
    let mut args = vec!["log", "-5", "-z", "--format=%H%x00%s", commit, "--"];
    args.extend_from_slice(paths);
    let text = git(root, &args)?;
    Ok(text
        .split_terminator('\0')
        .collect::<Vec<_>>()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|row| RecentCommit {
            commit: row[0].to_owned(),
            subject: row[1].to_owned(),
        })
        .collect())
}
