//! A built train's part order, passed to the installed Git pre-push hook.
use super::git;
use super::train::Manifest;
use pg_worker_core::run::record_io::atomic_write_private;
use std::path::{Path, PathBuf};
use std::process::Command;

const TRAIN_PREFIX: &str = "land/train-";

pub(super) fn is_train_branch(branch: &str) -> bool {
    branch
        .strip_prefix(TRAIN_PREFIX)
        .is_some_and(|name| !name.is_empty())
}

fn manifest_path(repo: &Path, branch: &str) -> Result<PathBuf, String> {
    if !is_train_branch(branch) {
        return Err("train push requires a land/train-* branch".into());
    }
    git::out(repo, &["check-ref-format", &format!("refs/heads/{branch}")])?;
    let common = git::out(
        repo,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    Ok(PathBuf::from(common)
        .join("plangraph/train-manifests")
        .join(format!("{branch}.heads")))
}

/// The heads file and the attributes file beside it; the push gate replays remerges with the attributes.
fn manifest_files(repo: &Path, branch: &str) -> Result<[PathBuf; 2], String> {
    let heads = manifest_path(repo, branch)?;
    let attrs = heads.with_extension("attrs");
    Ok([heads, attrs])
}

fn pending(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".next");
    PathBuf::from(name)
}

fn write_files(
    repo: &Path,
    branch: &str,
    manifest: &Manifest,
    pending_only: bool,
) -> Result<(), String> {
    let mut heads = String::new();
    for part in &manifest.parts {
        heads.push_str(&part.head);
        heads.push('\n');
    }
    let [heads_path, attrs_path] = manifest_files(repo, branch)?;
    for (path, text) in [
        (heads_path, heads),
        (attrs_path, manifest.policy.attributes.clone()),
    ] {
        let path = if pending_only { pending(&path) } else { path };
        atomic_write_private(&path, text.as_bytes())
            .map_err(|e| format!("cannot save push manifest for {branch}: {e}"))?;
    }
    Ok(())
}

pub(super) fn write_manifest(repo: &Path, branch: &str, manifest: &Manifest) -> Result<(), String> {
    write_files(repo, branch, manifest, false)
}

/// Saves the push manifest beside the live one without replacing it; `promote_manifest` makes it live after
/// the branch moved, so a failed write leaves the old branch and the old manifest together.
pub(super) fn stage_manifest(repo: &Path, branch: &str, manifest: &Manifest) -> Result<(), String> {
    write_files(repo, branch, manifest, true)
}

pub(super) fn promote_manifest(repo: &Path, branch: &str) -> Result<(), String> {
    for path in manifest_files(repo, branch)? {
        std::fs::rename(pending(&path), &path).map_err(|e| {
            format!(
                "cannot promote the push manifest of {branch}: {e}; move {} over {} by hand",
                pending(&path).display(),
                path.display()
            )
        })?;
    }
    Ok(())
}

pub(super) fn run(repo: &Path, branch: &str) -> Result<(), String> {
    let manifest = manifest_path(repo, branch)?;
    if !manifest.is_file() {
        return Err(format!(
            "train push manifest missing for {branch}; build the train first"
        ));
    }
    let reference = format!("refs/heads/{branch}");
    let local_env = git::out(repo, &["rev-parse", "--local-env-vars"])?;
    // Keep credentials and hook configuration, but keep the push in the chosen checkout.
    let mut command = Command::new("git");
    command.current_dir(repo);
    for variable in local_env.lines() {
        command.env_remove(variable);
    }
    let status = command
        .env("PLANGRAPH_TRAIN_MANIFEST", manifest)
        .env("PREFLIGHT_FAST", "1")
        .args([
            "push",
            "--no-follow-tags",
            "origin",
            &format!("{reference}:{reference}"),
        ])
        .status()
        .map_err(|e| format!("train push failed: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("train push failed: {status}"))
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::super::build::{build, publish};
    use super::super::build_tests::{g, part, Fx};
    use super::super::cli;

    #[test]
    fn train_push_uses_the_built_manifest_and_runs_the_hook() {
        let fx = Fx::new();
        let base = fx.base();
        let head = fx.commit(&base, "part.txt", "part\n");
        let manifest = build(&fx.input(vec![part(1, &head, None)], "", None))
            .unwrap()
            .manifest
            .unwrap();
        let branch = "land/train-fixture";
        publish(fx.path(), branch, &manifest).unwrap();

        let remote = tempfile::tempdir().unwrap();
        g(remote.path(), &["init", "-q", "--bare"]);
        g(
            fx.path(),
            &["remote", "add", "origin", remote.path().to_str().unwrap()],
        );

        let hook = fx.path().join(".git/hooks/pre-push");
        let script = format!(
            "#!/bin/sh\n\
             test \"$PREFLIGHT_FAST\" = 1 || exit 1\n\
             test \"$(cat \"$PLANGRAPH_TRAIN_MANIFEST\")\" = {head} || exit 1\n\
             touch .git/hook-ran\n"
        );
        pg_worker_core::executable_test_support::write_private_executable(&hook, script);
        let args = [
            "push",
            "--repo",
            fx.path().to_str().unwrap(),
            "--branch",
            branch,
        ]
        .map(str::to_owned);
        assert_eq!(cli::run(fx.path(), &args), 0);
        assert!(fx.path().join(".git/hook-ran").is_file());
        assert_eq!(g(remote.path(), &["rev-parse", branch]), manifest.train_sha);
    }
}
