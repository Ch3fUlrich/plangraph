//! Build and publish the host tooling from the synchronized Rust tree.
use super::{is_executable, plan, Env};
use std::ffi::OsString;
use std::io::{self, Write};
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
    let cargo = super::resolve_on_path("cargo", env::var_os("PATH").as_deref())
        .unwrap_or_else(|| home.join(".cargo/bin").join(executable("cargo")));
    let metadata = plan::workspace_metadata(&repo, &cargo)?;
    let (binaries, build_args) = prepare(&metadata, keeper, planner).map_err(io::Error::other)?;
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
    for (_, name) in &binaries {
        let path = executable(name);
        let out = bin.join(&path);
        publish(&artifacts.join(path), &out)?;
        println!("RELEASE INSTALLED {}", out.display());
        installed.push(name.clone());
    }
    // No stamp without every marked binary installed, so the next run tries again.
    plan::check_installed(&metadata, &installed, keeper).map_err(io::Error::other)?;
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
}
