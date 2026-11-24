//! The stamp of a release build: the Rust tree plus every file outside `rust/` that the Rust sources embed
//! (`include_str!`, `include_bytes!`), so a change to `deploy/exceptions.toml` or the workflow, which the
//! GO verifier carries inside its binary, changes the stamp and the host rebuilds.
use sha2::{Digest, Sha256};
use std::path::{Component, Path, PathBuf};
use std::process::Command;

fn git(repo: &Path, args: &[&str]) -> Result<String, String> {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    let out = command
        .current_dir(repo)
        .args(args)
        .output()
        .map_err(|e| format!("git {}: {e}", args.join(" ")))?;
    if !out.status.success() {
        return Err(format!(
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_owned())
}

/// The string literals that follow `include_str!(` / `include_bytes!(` in a source text.
pub fn embedded_literals(source: &str) -> Vec<String> {
    let mut out = Vec::new();
    for macro_name in ["include_str!", "include_bytes!"] {
        for (at, _) in source.match_indices(macro_name) {
            let rest = source[at + macro_name.len()..].trim_start();
            let Some(rest) = rest.strip_prefix('(') else {
                continue;
            };
            let Some(rest) = rest.trim_start().strip_prefix('"') else {
                continue;
            };
            if let Some(end) = rest.find('"') {
                out.push(rest[..end].to_owned());
            }
        }
    }
    out
}

/// `file`'s embedded path resolved against its directory, relative to the repository root; `None` when it
/// stays inside `rust/` (already covered by the tree) or leaves the repository.
fn outside_rust(file: &str, literal: &str) -> Option<String> {
    let mut parts: Vec<String> = Path::new(file)
        .parent()?
        .components()
        .filter_map(|c| match c {
            Component::Normal(n) => Some(n.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect();
    for component in Path::new(literal).components() {
        match component {
            Component::ParentDir => {
                parts.pop()?;
            }
            Component::Normal(n) => parts.push(n.to_string_lossy().into_owned()),
            _ => {}
        }
    }
    let path = PathBuf::from_iter(&parts).to_string_lossy().into_owned();
    (!path.starts_with("rust/")).then_some(path)
}

/// The sorted repository paths outside `rust/` that tracked Rust sources embed.
pub fn embedded_inputs(repo: &Path) -> Result<Vec<String>, String> {
    let listing = git(repo, &["ls-files", "--", "rust"])?;
    let mut paths = std::collections::BTreeSet::new();
    for file in listing.lines().filter(|f| f.ends_with(".rs")) {
        let Ok(text) = std::fs::read_to_string(repo.join(file)) else {
            continue;
        };
        paths.extend(
            embedded_literals(&text)
                .iter()
                .filter_map(|l| outside_rust(file, l)),
        );
    }
    Ok(paths.into_iter().collect())
}

/// The release stamp of `HEAD`: its `rust` tree, plus a digest of the committed blobs of the embedded inputs.
/// Returns the stamp and those input paths (the install refuses to build while one is modified).
pub fn release_stamp(repo: &Path) -> Result<(String, Vec<String>), String> {
    let tree = git(repo, &["rev-parse", "HEAD:rust"])?;
    let inputs = embedded_inputs(repo)?;
    let mut digest = Sha256::new();
    for path in &inputs {
        // A listed file missing from HEAD (an untracked or deleted input) is part of the stamp as such.
        let blob =
            git(repo, &["rev-parse", &format!("HEAD:{path}")]).unwrap_or_else(|_| "absent".into());
        digest.update(format!("{path} {blob}\n"));
    }
    let stamp = if inputs.is_empty() {
        tree
    } else {
        format!("{tree}+{}", &format!("{:x}", digest.finalize())[..16])
    };
    Ok((stamp, inputs))
}

/// SHA-256 (hex) of a file's bytes; `None` when it cannot be read.
pub fn sha256_file(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    Some(format!("{:x}", Sha256::digest(&bytes)))
}

/// Where the release installer records what it built, one file per binary under the state directory.
fn artifact_file(state_dir: &Path, bin: &str) -> PathBuf {
    state_dir.join("release-bins").join(bin)
}

/// Record that `bin` was built from release `stamp` into an artifact with hash `sha`. The binary counts as
/// live only where the file its consumer runs has exactly this hash.
pub fn record_artifact(state_dir: &Path, bin: &str, stamp: &str, sha: &str) -> std::io::Result<()> {
    let path = artifact_file(state_dir, bin);
    let dir = path
        .parent()
        .ok_or_else(|| std::io::Error::other("no parent"))?;
    std::fs::create_dir_all(dir)?;
    let mut staged = tempfile::NamedTempFile::new_in(dir)?;
    std::io::Write::write_all(&mut staged, format!("{stamp} {sha}\n").as_bytes())?;
    staged.persist(path).map_err(|e| e.error)?;
    Ok(())
}

/// `(stamp, sha)` recorded for `bin`.
pub fn read_artifact(state_dir: &Path, bin: &str) -> Option<(String, String)> {
    let text = std::fs::read_to_string(artifact_file(state_dir, bin)).ok()?;
    let (stamp, sha) = text.trim().split_once(' ')?;
    Some((stamp.to_owned(), sha.to_owned()))
}
