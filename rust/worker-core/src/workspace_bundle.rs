//! Bundle transport only. R5 must scan reachable content before execution/publication.
use crate::{
    hosts::RepoEntry,
    lockfile,
    workspace_resolver::{git_command, ResolveError},
};
use std::{
    ffi::OsStr,
    fs::{self, OpenOptions},
    path::Path,
    time::Duration,
};

fn git(root: &Path, args: &[&OsStr]) -> Result<String, ResolveError> {
    let out = git_command(root).args(args).output()?;
    if !out.status.success() {
        return Err(ResolveError::Git);
    }
    Ok(String::from_utf8(out.stdout)
        .map_err(|_| ResolveError::Git)?
        .trim()
        .into())
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 80
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
}

fn lock(repo: &RepoEntry, revision: &str) -> Result<lockfile::LockGuard, ResolveError> {
    if revision.len() != 40
        || !revision
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(ResolveError::Invalid("sha"));
    }
    let root = Path::new(&repo.root);
    let common = || -> Result<_, ResolveError> {
        Ok(root
            .join(git(
                root,
                &["rev-parse".as_ref(), "--git-common-dir".as_ref()],
            )?)
            .canonicalize()?)
    };
    let dir = common()?;
    let guard = lockfile::lock_with_retry(
        &dir.join("spawner-fetch.lock"),
        Duration::from_secs(30),
        &mut rand::rng(),
    )
    .map_err(|_| ResolveError::Lock)?;
    if common()? != dir {
        return Err(ResolveError::Verify("common_dir"));
    }
    if git(
        root,
        &[
            "remote".as_ref(),
            "get-url".as_ref(),
            "--all".as_ref(),
            "origin".as_ref(),
        ],
    )? != repo.remote
    {
        return Err(ResolveError::Verify("remote"));
    }
    Ok(guard)
}

/// Export unpushed history without pushing or overwriting a ref or output file.
pub fn export_bundle(
    repo: &RepoEntry,
    revision: &str,
    run_id: &str,
    output: &Path,
) -> Result<(), ResolveError> {
    export_with_cleanup(repo, revision, run_id, output, git)
}

fn export_with_cleanup(
    repo: &RepoEntry,
    revision: &str,
    run_id: &str,
    output: &Path,
    cleanup_ref: impl FnOnce(&Path, &[&OsStr]) -> Result<String, ResolveError>,
) -> Result<(), ResolveError> {
    if !valid_id(run_id) {
        return Err(ResolveError::Invalid("run_id"));
    }
    let _guard = lock(repo, revision)?;
    let root = Path::new(&repo.root);
    git(
        root,
        &[
            "-c".as_ref(),
            "core.hooksPath=".as_ref(),
            "fetch".as_ref(),
            "--recurse-submodules=no".as_ref(),
            "--".as_ref(),
            "origin".as_ref(),
        ],
    )?;
    let default = git(
        root,
        &["symbolic-ref".as_ref(), "refs/remotes/origin/HEAD".as_ref()],
    )?;
    if !default.starts_with("refs/remotes/origin/") || default == "refs/remotes/origin/HEAD" {
        return Err(ResolveError::Invalid("origin_default"));
    }
    let base = git(
        root,
        &["merge-base".as_ref(), revision.as_ref(), default.as_ref()],
    )?;
    if base == revision {
        return Err(ResolveError::Invalid("empty_bundle"));
    }
    let tip = format!("refs/spawner/bundle/{run_id}");
    git(
        root,
        &[
            "update-ref".as_ref(),
            tip.as_ref(),
            revision.as_ref(),
            "".as_ref(),
        ],
    )?;
    let result = (|| {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(output)?;
        let created = git_command(root)
            .args(["bundle", "create", "-", &tip, &format!("^{base}")])
            .stdout(file)
            .output();
        let result = match created {
            Ok(out) if out.status.success() => Ok(()),
            Ok(_) => Err(ResolveError::Git),
            Err(err) => Err(ResolveError::Io(err)),
        };
        if result.is_err() {
            fs::remove_file(output)?;
        }
        result
    })();
    let cleanup = cleanup_ref(
        root,
        &[
            "update-ref".as_ref(),
            "-d".as_ref(),
            tip.as_ref(),
            revision.as_ref(),
        ],
    );
    if let Err(err) = cleanup {
        if result.is_ok() {
            fs::remove_file(output)?;
        }
        return Err(err);
    }
    result
}

/// Import objects, never branches. Content scanning remains mandatory at the caller.
pub fn import_bundle(repo: &RepoEntry, revision: &str, bundle: &Path) -> Result<(), ResolveError> {
    let _guard = lock(repo, revision)?;
    let metadata = fs::symlink_metadata(bundle)?;
    if !metadata.file_type().is_file() {
        return Err(ResolveError::Invalid("bundle_file"));
    }
    if metadata.len() > 64 * 1024 * 1024 {
        return Err(ResolveError::BundleTooLarge);
    }
    // Git for Windows cannot consume Rust's verbatim canonical path as a bundle URL.
    let bundle = crate::workspace_launch::git_worktree_path(&bundle.canonicalize()?);
    let root = Path::new(&repo.root);
    git(
        root,
        &[
            "-c".as_ref(),
            "core.hooksPath=".as_ref(),
            "fetch".as_ref(),
            "--recurse-submodules=no".as_ref(),
            "--".as_ref(),
            "origin".as_ref(),
        ],
    )?;
    git(
        root,
        &["bundle".as_ref(), "verify".as_ref(), bundle.as_os_str()],
    )?;
    let heads = git(
        root,
        &["bundle".as_ref(), "list-heads".as_ref(), bundle.as_os_str()],
    )?;
    let fields: Vec<_> = heads.split_whitespace().collect();
    let tip = match fields.as_slice() {
        [sha, tip]
            if *sha == revision
                && tip
                    .strip_prefix("refs/spawner/bundle/")
                    .is_some_and(valid_id) =>
        {
            *tip
        }
        _ => return Err(ResolveError::Invalid("bundle_heads")),
    };
    git(
        root,
        &[
            "-c".as_ref(),
            "core.hooksPath=".as_ref(),
            "fetch".as_ref(),
            "--no-tags".as_ref(),
            "--no-write-fetch-head".as_ref(),
            "--recurse-submodules=no".as_ref(),
            "--".as_ref(),
            bundle.as_os_str(),
            tip.as_ref(),
        ],
    )?;
    git(
        root,
        &[
            "cat-file".as_ref(),
            "-e".as_ref(),
            format!("{revision}^{{commit}}").as_ref(),
        ],
    )?;
    Ok(())
}

#[cfg(test)]
#[path = "workspace_bundle_tests.rs"]
mod tests;
