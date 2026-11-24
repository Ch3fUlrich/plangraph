//! Where the automatic fix command runs. Cargo builds the pull request's own code (`build.rs`, procedural
//! macros), so the command runs under a Landlock jail (`pg-jail`): it reads the system, the toolchain and the
//! dependency caches, and writes only its own checkout and scratch directories. The trusted clone, the watcher's
//! HOME, its credentials and everything else are denied by the kernel; the credential paths of the watcher are
//! named so that a grant covering one refuses the fix; the child gets an empty network namespace and an emptied
//! environment. A host that cannot enforce the jail refuses the fix, which then stays with the fix writer.
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
    // Of /etc only what the loader, `getpwuid` and the time zone need: a token file can lie anywhere else.
    let mut readable: Vec<PathBuf> = [
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/dev",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/alternatives",
        "/etc/localtime",
        "/etc/passwd",
        "/etc/group",
        "/etc/nsswitch.conf",
    ]
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
        deny_read: credential_paths(real_home.as_deref(), real_cargo.as_deref()),
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
    // SAFETY: the closure only confines the child (a network namespace and Landlock rules) before it execs; it
    // touches no watcher state.
    let maps = IdMaps::current();
    unsafe {
        command.pre_exec(move || {
            if let Err(error) = maps.isolate_network() {
                // A test host may forbid user namespaces; a real run never goes on without the network cut off.
                if !cfg!(test) {
                    return Err(error);
                }
            }
            pg_jail::apply(&spec).map_err(|e| std::io::Error::other(e.to_string()))
        });
    }
    pg_worker_core::host_command::output(&mut command, Duration::from_secs(1800))
        .map(drop)
        .map_err(|_| format!("{program} failed in the jail"))
}

/// Where the watcher keeps what the fix command must never read; each existing path is named to the jail, which
/// refuses to start when an allow rule covers one.
#[cfg(target_os = "linux")]
fn credential_paths(home: Option<&Path>, cargo_home: Option<&Path>) -> Vec<std::path::PathBuf> {
    let mut paths: Vec<std::path::PathBuf> = std::env::var_os("PG_TOKEN_FILE")
        .map(std::path::PathBuf::from)
        .into_iter()
        .collect();
    if let Some(home) = home {
        for name in [
            ".config/gh",
            ".config/plangraph",
            ".ssh",
            ".netrc",
            ".git-credentials",
            ".gnupg",
        ] {
            paths.push(home.join(name));
        }
    }
    if let Some(cargo) = cargo_home {
        for name in ["credentials", "credentials.toml", "config.toml"] {
            paths.push(cargo.join(name));
        }
    }
    paths.retain(|path| path.exists());
    paths
}

/// The ids the child keeps in its own user namespace: itself, so the files it may use stay usable.
#[cfg(target_os = "linux")]
struct IdMaps {
    uid: std::ffi::CString,
    gid: std::ffi::CString,
}

#[cfg(target_os = "linux")]
impl IdMaps {
    fn current() -> Self {
        // SAFETY: getuid and getgid cannot fail and touch no memory.
        let (uid, gid) = unsafe { (libc::getuid(), libc::getgid()) };
        let map = |id: u32| std::ffi::CString::new(format!("{id} {id} 1")).expect("digits");
        Self {
            uid: map(uid),
            gid: map(gid),
        }
    }

    /// Runs in the forked child before exec: a new user and network namespace, which holds loopback only.
    fn isolate_network(&self) -> std::io::Result<()> {
        // SAFETY: plain system calls on this process; the paths and data outlive them.
        unsafe {
            if libc::unshare(libc::CLONE_NEWUSER | libc::CLONE_NEWNET) != 0 {
                return Err(std::io::Error::last_os_error());
            }
        }
        write_proc(c"/proc/self/setgroups", b"deny")?;
        write_proc(c"/proc/self/uid_map", self.uid.as_bytes())?;
        write_proc(c"/proc/self/gid_map", self.gid.as_bytes())
    }
}

#[cfg(target_os = "linux")]
fn write_proc(path: &std::ffi::CStr, data: &[u8]) -> std::io::Result<()> {
    // SAFETY: open, write and close on a descriptor this function owns.
    unsafe {
        let fd = libc::open(path.as_ptr(), libc::O_WRONLY);
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let written = libc::write(fd, data.as_ptr().cast(), data.len());
        let error = std::io::Error::last_os_error();
        libc::close(fd);
        if written < 0 || written as usize != data.len() {
            return Err(error);
        }
    }
    Ok(())
}
