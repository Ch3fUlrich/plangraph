//! Git freshness, writer-family trailer extraction, and per-file diff helpers for `plangraph review`.
use crate::base_fresh::{check_ref, python_command};
use pg_worker_core::review_split::valid_diff_path;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone)]
pub struct ResolvedRange {
    pub repo_root: PathBuf,
    pub git_common_dir: PathBuf,
    pub base_sha: String,
    pub head_sha: String,
}

fn git_stdout(repo: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let out = Command::new("git")
        .arg("--literal-pathspecs")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .map_err(|e| format!("failed to run git {}: {e}", args.join(" ")))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_owned();
        return Err(if err.is_empty() {
            format!(
                "git {} exited with {}",
                args.join(" "),
                out.status.code().unwrap_or(1)
            )
        } else {
            format!("git {}: {err}", args.join(" "))
        });
    }
    Ok(out.stdout)
}

fn git_text(repo: &Path, args: &[&str]) -> Result<String, String> {
    let bytes = git_stdout(repo, args)?;
    Ok(String::from_utf8_lossy(&bytes).trim().to_owned())
}

fn resolve_commit(repo: &Path, rev: &str) -> Result<String, String> {
    let spec = format!("{rev}^{{commit}}");
    git_text(repo, &["rev-parse", "--verify", &spec])
}

/// Resolve repo paths, verify D-5661 freshness against `origin`, and ensure a non-empty diff.
pub fn resolve_and_check_fresh(
    repo: &Path,
    base: &str,
    head: &str,
    fetch_origin: bool,
) -> Result<ResolvedRange, String> {
    let top = git_text(repo, &["rev-parse", "--show-toplevel"])?;
    let repo_root = PathBuf::from(&top)
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(&top));
    let common_raw = git_text(repo, &["rev-parse", "--git-common-dir"])?;
    let common_path = Path::new(&common_raw);
    let git_common_dir = if common_path.is_absolute() {
        common_path.to_path_buf()
    } else {
        repo_root.join(common_path)
    }
    .canonicalize()
    .unwrap_or_else(|_| repo_root.join(".git"));

    let head_sha = resolve_commit(repo, head)?;
    let base_sha = resolve_commit(repo, base)?;

    // The one D-5661 gate (plangraph.spawner.base_fresh) decides; it refuses when no origin ref resolves.
    check_ref(
        &repo_root.to_string_lossy(),
        &head_sha,
        fetch_origin,
        Some(&repo_root),
    )
    .map_err(|reason| format!("refused: {reason}"))?;

    let changed = changed_names(repo, &base_sha, &head_sha)?;
    if changed.iter().all(|&b| b == 0) {
        return Err(format!(
            "refused: empty_diff: no changed files in {}..{}",
            &base_sha[..12.min(base_sha.len())],
            &head_sha[..12.min(head_sha.len())]
        ));
    }

    Ok(ResolvedRange {
        repo_root,
        git_common_dir,
        base_sha,
        head_sha,
    })
}

/// The commits of one writer-family set: the review seat for them comes from outside `families`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriterGroup {
    pub families: Vec<String>,
    pub commits: Vec<String>,
}

/// Group commits by their writer family set, in first-seen order. A commit with no resolved
/// family joins the first group, so a seat still reviews it. One family exclusion applies per
/// group, never per range.
pub fn group_by_families(per_commit: &[(String, BTreeSet<String>)]) -> Vec<WriterGroup> {
    let mut groups: Vec<WriterGroup> = Vec::new();
    let mut unresolved = Vec::new();
    for (sha, families) in per_commit {
        if families.is_empty() {
            unresolved.push(sha.clone());
            continue;
        }
        let families: Vec<String> = families.iter().cloned().collect();
        match groups.iter_mut().find(|g| g.families == families) {
            Some(group) => group.commits.push(sha.clone()),
            None => groups.push(WriterGroup {
                families,
                commits: vec![sha.clone()],
            }),
        }
    }
    if let Some(first) = groups.first_mut() {
        first.commits.extend(unresolved);
    }
    groups
}

/// The writer groups of `base..head`; one group when a single writer family set wrote the range.
pub fn writer_groups(
    repo: &Path,
    base_sha: &str,
    head_sha: &str,
) -> Result<Vec<WriterGroup>, String> {
    let read =
        pg_worker_core::writer_families::commit_families(repo, base_sha, head_sha, &[], &|| {
            python_command(Some(repo))
        })?;
    let groups = group_by_families(&read.per_commit);
    Ok(groups)
}

// Same flags as pg-spawnd's inline diff (prompt_git.rs), so the CLI measures what the seat gets.
const DIFF_FLAGS: [&str; 5] = [
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--binary",
];

fn changed_names(repo: &Path, base_sha: &str, head_sha: &str) -> Result<Vec<u8>, String> {
    git_stdout(
        repo,
        &[
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            &format!("{base_sha}...{head_sha}"),
            "--",
        ],
    )
}

/// Read per-file diff byte payloads for `base_sha` to `head_sha`, as the spawner inlines them.
pub fn measure_diff_files(
    repo: &Path,
    base_sha: &str,
    head_sha: &str,
) -> Result<Vec<(String, Vec<u8>)>, String> {
    let raw_names = changed_names(repo, base_sha, head_sha)?;
    let mut out = Vec::new();
    for part in raw_names.split(|&b| b == 0).filter(|p| !p.is_empty()) {
        let path = String::from_utf8(part.to_vec())
            .map_err(|_| "refused: non-utf8 path in diff".to_owned())?;
        if !valid_diff_path(&path) {
            return Err(format!("refused: invalid diff path: {path}"));
        }
        let mut args: Vec<&str> = DIFF_FLAGS.to_vec();
        let range = format!("{base_sha}...{head_sha}");
        args.extend([range.as_str(), "--", &path]);
        let bytes = git_stdout(repo, &args)?;
        out.push((path, bytes));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn commit(sha: &str, families: &[&str]) -> (String, BTreeSet<String>) {
        (sha.into(), families.iter().map(|f| f.to_string()).collect())
    }

    #[test]
    fn commits_group_by_writer_family_set_in_first_seen_order() {
        let groups = group_by_families(&[
            commit("c1", &["openai"]),
            commit("c2", &["google"]),
            commit("c3", &["openai"]),
            commit("c4", &["anthropic", "google"]),
            commit("c5", &[]),
        ]);
        let got: Vec<(Vec<&str>, Vec<&str>)> = groups
            .iter()
            .map(|g| {
                (
                    g.families.iter().map(String::as_str).collect(),
                    g.commits.iter().map(String::as_str).collect(),
                )
            })
            .collect();
        assert_eq!(
            got,
            vec![
                (vec!["openai"], vec!["c1", "c3", "c5"]), // c5 has no family: it joins the first group
                (vec!["google"], vec!["c2"]),
                (vec!["anthropic", "google"], vec!["c4"]),
            ]
        );
        assert_eq!(
            group_by_families(&[commit("a", &["openai"]), commit("b", &["openai"])]).len(),
            1
        );
    }
}
