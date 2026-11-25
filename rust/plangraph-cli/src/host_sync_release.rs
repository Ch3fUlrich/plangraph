//! Build and publish the host tooling from the synchronized Rust tree.
use super::{is_executable, plan, Env};
use crate::runner::download::{sha256_file, valid_hash};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::{env, fs};

pub(super) fn git(repo: &Path, args: &[&str]) -> io::Result<String> {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    let output = command.current_dir(repo).args(args).output()?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().into())
}

fn publish(source: &Path, destination: &Path) -> io::Result<String> {
    let mut staged = tempfile::NamedTempFile::new_in(destination.parent().unwrap())?;
    let mut input = fs::File::open(source)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0; 64 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let bytes = &buffer[..count];
        staged.write_all(bytes)?;
        digest.update(bytes);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        staged
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o755))?;
    }
    staged.as_file().sync_all()?;
    staged.persist(destination).map_err(|err| err.error)?;
    if !is_executable(destination) {
        return Err(io::Error::other(format!(
            "{} is not executable",
            destination.display()
        )));
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ReleaseReceipt {
    schema: u8,
    build_head: String,
    rust_tree: String,
    binaries: BTreeMap<String, String>,
}

fn object_id(value: &str) -> bool {
    matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn revision(repo: &Path) -> io::Result<(String, String)> {
    let output = git(repo, &["rev-parse", "HEAD", "HEAD:rust"])?;
    let (head, tree) = output
        .split_once('\n')
        .filter(|(head, tree)| object_id(head) && object_id(tree))
        .ok_or_else(|| io::Error::other("unrecognized checkout revision"))?;
    Ok((head.into(), tree.into()))
}

fn stamp_path(state: &Path, keeper: bool) -> PathBuf {
    state.join(if keeper {
        "release-keeper-built-tree"
    } else {
        "release-built-tree"
    })
}

fn state_path(home: &Path) -> PathBuf {
    env::var_os("XDG_STATE_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/state"))
        .join("plangraph")
}

fn load_receipt(stamp: &Path, tree: &str) -> io::Result<ReleaseReceipt> {
    let receipt: ReleaseReceipt = serde_json::from_slice(&fs::read(stamp.with_extension("json"))?)?;
    if receipt.schema != 1
        || !object_id(&receipt.build_head)
        || receipt.rust_tree != tree
        || receipt.binaries.is_empty()
        || receipt.binaries.iter().any(|(name, digest)| {
            name.is_empty()
                || matches!(name.as_str(), "." | "..")
                || !name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte))
                || !valid_hash(digest)
        })
    {
        return Err(io::Error::other(
            "release receipt differs from the Rust tree or is invalid",
        ));
    }
    Ok(receipt)
}

type Planner = fn(&serde_json::Value, bool) -> Result<Vec<plan::Binary>, String>;

/// The binaries to install and the cargo arguments that build them, checked against the workspace
/// marks. `planner` is the generator under check; the check does not trust it.
fn prepare(
    metadata: &serde_json::Value,
    keeper: bool,
    planner: Planner,
) -> Result<(Vec<plan::Binary>, Vec<String>), String> {
    let binaries = planner(metadata, keeper)?;
    let args = plan::build_args(&binaries);
    plan::check_build(metadata, &args, keeper)?;
    Ok((binaries, args))
}

fn install(repo: &Path, home: &Path, keeper: bool) -> io::Result<()> {
    install_with(repo, home, keeper, plan::plan)
}

fn install_with(repo: &Path, home: &Path, keeper: bool, planner: Planner) -> io::Result<()> {
    let repo = repo.canonicalize()?;
    let bin = home.join(".local/bin");
    let state = state_path(home);
    fs::create_dir_all(&state)?;
    let _lock = pg_worker_core::lockfile::try_lock(&state.join("release-install.lock"))
        .map_err(io::Error::other)?
        .ok_or_else(|| io::Error::other("another host release install is running"))?;
    let stamp = stamp_path(&state, keeper);
    let (build_head, tree) = revision(&repo)?;
    let cargo = super::resolve_on_path("cargo", env::var_os("PATH").as_deref())
        .unwrap_or_else(|| home.join(".cargo/bin").join(executable("cargo")));
    let metadata = plan::workspace_metadata(&repo, &cargo)?;
    let (binaries, build_args) = prepare(&metadata, keeper, planner).map_err(io::Error::other)?;
    if fs::read_to_string(&stamp).is_ok_and(|saved| saved.trim() == tree)
        && load_receipt(&stamp, &tree).is_ok_and(|receipt| {
            receipt.binaries.len() == binaries.len()
                && binaries
                    .iter()
                    .all(|(_, name)| receipt.binaries.contains_key(&executable(name)))
                && verify_installed(&bin, &receipt).is_ok()
        })
    {
        println!("SAME release {tree}");
        return Ok(());
    }
    if !git(&repo, &["status", "--porcelain", "--", "rust"])?.is_empty() {
        return Err(io::Error::other("Rust checkout has local changes"));
    }
    let target = env::var_os("CARGO_TARGET_DIR")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| io::Error::other("CARGO_TARGET_DIR must name the shared target"))?;
    let target = repo.join("rust").join(target);
    let mut build = Command::new(cargo);
    build.current_dir(repo.join("rust")).args(&build_args);
    let status = build.status()?;
    if !status.success() {
        return Err(io::Error::other(format!("release build: {status}")));
    }
    if git(&repo, &["rev-parse", "HEAD:rust"])? != tree
        || !git(&repo, &["status", "--porcelain", "--", "rust"])?.is_empty()
    {
        return Err(io::Error::other(
            "Rust checkout changed during release build",
        ));
    }
    let artifacts = target.join("release");
    for (_, name) in &binaries {
        let path = artifacts.join(executable(name));
        if !is_executable(&path) {
            return Err(io::Error::other(format!(
                "missing build artifact {}",
                path.display()
            )));
        }
    }
    fs::create_dir_all(&bin)?;
    let mut installed = Vec::new();
    let mut digests = BTreeMap::new();
    for (_, name) in &binaries {
        let path = executable(name);
        let out = bin.join(&path);
        let digest = publish(&artifacts.join(&path), &out)?;
        digests.insert(path, digest);
        println!("RELEASE INSTALLED {}", out.display());
        installed.push(name.clone());
    }
    // No stamp without every marked binary installed, so the next run tries again.
    plan::check_installed(&metadata, &installed, keeper).map_err(io::Error::other)?;
    let receipt = ReleaseReceipt {
        schema: 1,
        build_head,
        rust_tree: tree.clone(),
        binaries: digests,
    };
    pg_worker_core::run::record_io::atomic_write_private(
        &stamp.with_extension("json"),
        &serde_json::to_vec(&receipt)?,
    )?;
    let mut staged = tempfile::NamedTempFile::new_in(&state)?;
    writeln!(staged, "{tree}")?;
    staged.as_file().sync_all()?;
    staged.persist(stamp).map_err(|err| err.error)?;
    Ok(())
}

fn executable(name: &str) -> String {
    format!("{name}{}", env::consts::EXE_SUFFIX)
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let (repo, keeper) = match args {
        [repo] => (repo, false),
        [repo, flag] if flag == "--with-keeper-backup" => (repo, true),
        _ => {
            eprintln!("usage: host-sync --install-release REPO [--with-keeper-backup]");
            return 2;
        }
    };
    let result = Env::from_process()
        .home
        .ok_or_else(|| io::Error::other("HOME is not set"))
        .and_then(|home| install(Path::new(repo), &home, keeper));
    match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("FAIL release install: {error}");
            1
        }
    }
}

fn verify_installed(bin: &Path, receipt: &ReleaseReceipt) -> io::Result<()> {
    for (name, digest) in &receipt.binaries {
        let path = bin.join(name);
        if !fs::symlink_metadata(&path).is_ok_and(|meta| meta.is_file())
            || !is_executable(&path)
            || sha256_file(&path).map_err(io::Error::other)? != *digest
        {
            return Err(io::Error::other(format!(
                "installed binary differs: {name}"
            )));
        }
    }
    Ok(())
}

fn readback(
    repo: &Path,
    expected: &str,
    home: &Path,
    keeper: bool,
) -> io::Result<serde_json::Value> {
    if !object_id(expected) {
        return Err(io::Error::other("expected a full checkout commit ID"));
    }
    let state = state_path(home);
    let lock = fs::File::open(state.join("release-install.lock"))?;
    lock.try_lock_shared()
        .map_err(|err| io::Error::other(format!("release install lock: {err}")))?;
    let (head, tree) = revision(repo)?;
    if head != expected {
        return Err(io::Error::other(
            "checkout HEAD differs from the requested commit",
        ));
    }
    let stamp = stamp_path(&state, keeper);
    if fs::read_to_string(&stamp)?.trim() != tree {
        return Err(io::Error::other(
            "release stamp differs from the checkout Rust tree",
        ));
    }
    let receipt = load_receipt(&stamp, &tree)?;
    verify_installed(&home.join(".local/bin"), &receipt)?;
    if git(repo, &["rev-parse", "HEAD"])? != head {
        return Err(io::Error::other(
            "checkout HEAD moved during release readback",
        ));
    }
    Ok(serde_json::json!({
        "status": "current",
        "schema": receipt.schema,
        "head": head,
        "build_head": receipt.build_head,
        "rust_tree": tree,
        "binaries": receipt.binaries,
    }))
}

pub(super) fn check(args: &[OsString]) -> i32 {
    let (repo, head, keeper) = match args {
        [repo, head] => (repo, head, false),
        [repo, head, flag] if flag == "--with-keeper-backup" => (repo, head, true),
        _ => {
            eprintln!("usage: host-sync --check-release REPO HEAD [--with-keeper-backup]");
            return 2;
        }
    };
    let result = Env::from_process()
        .home
        .zip(head.to_str())
        .ok_or_else(|| io::Error::other("HOME and a full checkout commit ID are required"))
        .and_then(|(home, head)| readback(Path::new(repo), head, &home, keeper));
    match result {
        Ok(receipt) => {
            println!("{receipt}");
            0
        }
        Err(error) => {
            eprintln!("FAIL release readback: {error}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn metadata() -> serde_json::Value {
        let package = |name: &str, mark: &[&str]| {
            json!({"name": name, "targets": [{"name": name, "kind": ["bin"]}],
                "metadata": {"plangraph": {"host-binaries": mark}}})
        };
        json!({"packages": [package("pg-a", &["pg-a"]), package("pg-telemetry", &["pg-telemetry"])]})
    }

    #[test]
    fn the_installer_refuses_a_plan_that_leaves_a_marked_binary_out() {
        let (binaries, args) = prepare(&metadata(), false, plan::plan).unwrap();
        assert_eq!(binaries.len(), 2);
        assert!(args.windows(2).any(|w| w == ["--bin", "pg-telemetry"]));
        // A generator that drops the new crate is caught by the marks, not by itself.
        let forgetful: Planner = |metadata, keeper| {
            let mut all = plan::plan(metadata, keeper)?;
            all.retain(|(_, name)| name != "pg-telemetry");
            Ok(all)
        };
        let error = prepare(&metadata(), false, forgetful).unwrap_err();
        assert!(error.contains("pg-telemetry"), "{error}");
    }

    #[test]
    fn the_installer_refuses_a_mark_of_the_wrong_type_before_it_builds() {
        let mut bad = metadata();
        bad["packages"][1]["metadata"]["plangraph"]["host-binaries"] = json!("pg-telemetry");
        let error = prepare(&bad, false, plan::plan).unwrap_err();
        assert!(error.contains("host-binaries must be a list"), "{error}");
    }
}
