//! Build and publish the host tooling from the synchronized Rust tree.
use super::{is_executable, Env};
use std::ffi::OsString;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::{env, fs};

const BINARIES: &[(&str, &str)] = &[
    ("pg-spawnd", "pg-spawnd"),
    ("plangraph-cli", "plangraph"),
    ("pg-worker-core", "pg-disk-hygiene"),
    ("pg-worker-core", "turn-budget-hook"),
];

fn git(repo: &Path, args: &[&str]) -> io::Result<String> {
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

fn install(repo: &Path, home: &Path, keeper: bool) -> io::Result<()> {
    let mut binaries = BINARIES.to_vec();
    if keeper {
        binaries.push(("pg-keeper-backup", "pg-keeper-backup"));
    }
    let repo = repo.canonicalize()?;
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
    let tree = git(&repo, &["rev-parse", "HEAD:rust"])?;
    if fs::read_to_string(&stamp).is_ok_and(|saved| saved.trim() == tree)
        && binaries
            .iter()
            .all(|(_, name)| is_executable(&bin.join(executable(name))))
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
    let cargo = super::resolve_on_path("cargo", env::var_os("PATH").as_deref())
        .unwrap_or_else(|| home.join(".cargo/bin").join(executable("cargo")));
    let mut build = Command::new(cargo);
    build
        .current_dir(repo.join("rust"))
        .args(["build", "--release", "--locked"]);
    let packages: std::collections::BTreeSet<_> =
        binaries.iter().map(|(package, _)| *package).collect();
    for package in packages {
        build.args(["-p", package]);
    }
    for (_, name) in &binaries {
        build.args(["--bin", *name]);
    }
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
    for (_, name) in &binaries {
        let path = executable(name);
        let out = bin.join(&path);
        publish(&artifacts.join(path), &out)?;
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
