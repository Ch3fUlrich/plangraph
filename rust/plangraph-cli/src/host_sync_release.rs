//! Build and publish the host tooling from the synchronized Rust tree.
use super::{is_executable, Env};
use crate::deploy_manifest::stamp::release_stamp;
use crate::deploy_manifest::{this_host_roles, Manifest};
use std::ffi::OsString;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::{env, fs};

/// The binaries this host installs: the manifest's deployed binaries of the host's roles, plus the
/// keeper role when `--with-keeper-backup` is given.
fn binaries(repo: &Path, home: &Path, keeper: bool) -> io::Result<Vec<(String, String)>> {
    let manifest = Manifest::load_from_repo(repo).map_err(io::Error::other)?;
    let config = env::var_os("XDG_CONFIG_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"))
        .join("plangraph");
    let mut roles = this_host_roles(&config).map_err(io::Error::other)?;
    if keeper {
        roles.insert("keeper".into());
    }
    Ok(manifest
        .binaries_for(&roles)
        .into_iter()
        .map(|(package, bin)| (package.to_owned(), bin.to_owned()))
        .collect())
}

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

pub(super) fn publish(source: &Path, destination: &Path) -> io::Result<()> {
    let mut staged = tempfile::NamedTempFile::new_in(destination.parent().unwrap())?;
    let mut input = fs::File::open(source)?;
    io::copy(&mut input, &mut staged)?;
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
    Ok(())
}

pub(super) fn install(repo: &Path, home: &Path, keeper: bool) -> io::Result<()> {
    let repo = repo.canonicalize()?;
    let binaries = binaries(&repo, home, keeper)?;
    let bin = home.join(".local/bin");
    let state = env::var_os("XDG_STATE_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/state"))
        .join("plangraph");
    fs::create_dir_all(&state)?;
    let _lock = pg_worker_core::lockfile::try_lock(&state.join("release-install.lock"))
        .map_err(io::Error::other)?
        .ok_or_else(|| io::Error::other("another host release install is running"))?;
    let stamp = state.join(if keeper {
        "release-keeper-built-tree"
    } else {
        "release-built-tree"
    });
    // The stamp covers the Rust tree and the files outside it that the sources embed, so an exceptions-only
    // change is a new release.
    let (tree, embedded) = release_stamp(&repo).map_err(io::Error::other)?;
    let dirty = |repo: &Path| -> io::Result<bool> {
        let mut args = vec!["status", "--porcelain", "--", "rust"];
        args.extend(embedded.iter().map(String::as_str));
        Ok(!git(repo, &args)?.is_empty())
    };
    // The host-wide stamp alone is not enough: every binary needs its own record of this release, made
    // from the artifact that is installed, so an older or excluded binary cannot ride on a current stamp.
    let recorded = |(_, name): &(String, String)| {
        let file = bin.join(executable(name));
        crate::deploy_manifest::stamp::read_artifact(&state, name).is_some_and(|(at, sha)| {
            at == tree
                && is_executable(&file)
                && crate::deploy_manifest::stamp::sha256_file(&file).as_deref()
                    == Some(sha.as_str())
        })
    };
    if fs::read_to_string(&stamp).is_ok_and(|saved| saved.trim() == tree)
        && binaries.iter().all(recorded)
    {
        println!("SAME release {tree}");
        return Ok(());
    }
    if dirty(&repo)? {
        return Err(io::Error::other("Rust checkout has local changes"));
    }
    let target = env::var_os("CARGO_TARGET_DIR")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| io::Error::other("CARGO_TARGET_DIR must name the shared target"))?;
    let target = repo.join("rust").join(target);
    let cargo = super::resolve_on_path("cargo", env::var_os("PATH").as_deref())
        .unwrap_or_else(|| home.join(".cargo/bin").join(executable("cargo")));
    let mut build = Command::new(cargo);
    build
        .current_dir(repo.join("rust"))
        .args(["build", "--release", "--locked"]);
    let packages: std::collections::BTreeSet<_> = binaries
        .iter()
        .map(|(package, _)| package.as_str())
        .collect();
    for package in packages {
        build.args(["-p", package]);
    }
    for (_, name) in &binaries {
        build.args(["--bin", name]);
    }
    let status = build.status()?;
    if !status.success() {
        return Err(io::Error::other(format!("release build: {status}")));
    }
    if release_stamp(&repo).map_err(io::Error::other)?.0 != tree || dirty(&repo)? {
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
    for (_, name) in &binaries {
        let path = executable(name);
        let out = bin.join(&path);
        let built = artifacts.join(&path);
        publish(&built, &out)?;
        // What was built, hashed once at the source: the file at the path each consumer runs must match it.
        let sha = crate::deploy_manifest::stamp::sha256_file(&built)
            .ok_or_else(|| io::Error::other(format!("cannot read {}", built.display())))?;
        crate::deploy_manifest::stamp::record_artifact(&state, name, &tree, &sha)?;
        println!("INSTALLED {}", out.display());
    }
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
