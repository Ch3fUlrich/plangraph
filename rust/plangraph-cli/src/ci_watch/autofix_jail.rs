//! Where the automatic fix command runs. Cargo builds the pull request's own code (`build.rs`, procedural
//! macros), so the command runs under a Landlock jail (`pg-jail`): it reads the system, the toolchain and the
//! dependency caches, and writes only its own checkout and scratch directories. The trusted clone, the watcher's
//! HOME, its credentials and everything else are denied by the kernel, and the environment is emptied besides.
//! A host that cannot enforce the jail refuses the fix, which then stays with the fix writer.
use super::Result;
use std::path::Path;

#[cfg(not(target_os = "linux"))]
pub fn run(_: &Path, _: &str, _: &str, _: &[&str], _: &Path) -> Result<()> {
    Err("no filesystem jail on this host; the fix is left to the writer".into())
}

/// Run `program` in `sandbox/dir` jailed. `scratch` holds the HOME, CARGO_HOME, target and temporary directories.
#[cfg(target_os = "linux")]
pub fn run(sandbox: &Path, dir: &str, program: &str, args: &[&str], scratch: &Path) -> Result<()> {
    use std::os::unix::process::CommandExt;
    use std::path::PathBuf;
    use std::process::Command;
    use std::time::Duration;

    let [home, cargo_home, tmp, target] =
        ["home", "cargo-home", "tmp", "target"].map(|name| scratch.join(name));
    for path in [&home, &cargo_home, &tmp, &target] {
        std::fs::create_dir_all(path).map_err(|e| e.to_string())?;
    }
    let env_path = |name: &str| std::env::var_os(name).map(PathBuf::from);
    let real_home = env_path("HOME");
    let real_cargo =
        env_path("CARGO_HOME").or_else(|| real_home.as_ref().map(|h| h.join(".cargo")));
    let rustup = env_path("RUSTUP_HOME").or_else(|| real_home.as_ref().map(|h| h.join(".rustup")));
    let mut readable: Vec<PathBuf> = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/dev"]
        .iter()
        .map(PathBuf::from)
        .collect();
    readable.extend(rustup.clone());
    // The dependency caches and the cargo/rustup shims are shared read-only, through links in the scratch CARGO_HOME.
    for shared in ["registry", "git", "bin"] {
        if let Some(source) = real_cargo.as_ref().map(|c| c.join(shared)) {
            if let Ok(source) = source.canonicalize() {
                let _ = std::os::unix::fs::symlink(&source, cargo_home.join(shared));
                readable.push(source);
            }
        }
    }
    readable.retain(|path| path.exists());
    let spec = pg_jail::JailSpec {
        rw: vec![
            sandbox.to_path_buf(),
            home.clone(),
            cargo_home.clone(),
            tmp.clone(),
            target.clone(),
            PathBuf::from("/dev/null"),
        ],
        ro: readable,
        deny_read: Vec::new(),
        proc_self: true,
    };
    let mut command = Command::new(program);
    command
        .args(args)
        .current_dir(sandbox.join(dir))
        .env_clear()
        .env("HOME", &home)
        .env("CARGO_HOME", &cargo_home)
        .env("CARGO_TARGET_DIR", &target)
        .env("CARGO_NET_OFFLINE", "true")
        .env("TMPDIR", &tmp)
        .env("GIT_TERMINAL_PROMPT", "0");
    // Only the search path and the toolchain's location cross from the watcher.
    for name in ["PATH", "RUSTUP_TOOLCHAIN"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    if let Some(rustup) = rustup {
        command.env("RUSTUP_HOME", rustup);
    }
    // SAFETY: the closure only restricts the child (Landlock rules) before it execs; it touches no watcher state.
    unsafe {
        command.pre_exec(move || {
            pg_jail::apply(&spec).map_err(|e| std::io::Error::other(e.to_string()))
        });
    }
    pg_worker_core::host_command::output(&mut command, Duration::from_secs(1800))
        .map(drop)
        .map_err(|_| format!("{program} failed in the jail"))
}
