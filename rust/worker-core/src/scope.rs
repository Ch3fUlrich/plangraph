//! Transient systemd scope around a jailed harness child.
//!
//! Two environments by design: `systemd-run` must see the caller's (it reaches
//! the user manager over `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS`), while
//! the agent child must see only what the harness set on the original command.

use std::path::{Path, PathBuf};
use std::process::Command;

/// Ceilings for the transient scope (`systemd-run --property`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopeLimits {
    /// e.g. `"2G"`; `MemorySwapMax=0` is always pinned alongside it so the
    /// ceiling cannot be blitted away through swap.
    pub memory_max: String,
    pub tasks_max: u32,
}

impl Default for ScopeLimits {
    fn default() -> Self {
        ScopeLimits {
            memory_max: "2G".into(),
            tasks_max: 256,
        }
    }
}

/// Filesystem the child sees: `rw` writable, `ro` readable. Enforcement is
/// `pg-jail`'s; this only carries the request.
pub struct Jail<'a> {
    pub bin: &'a Path,
    pub rw: Vec<PathBuf>,
    pub ro: Vec<PathBuf>,
}

/// Read-only roots a worktree needs, existing ones only: a host without
/// `/lib64` or `/opt` must not be handed a dangling path.
pub fn system_ro() -> Vec<PathBuf> {
    const CANDIDATES: &[&str] = &[
        "/usr",
        "/usr/local",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc",
        "/dev",
        "/opt",
        "/run/systemd/resolve",
    ];
    CANDIDATES
        .iter()
        .filter_map(|p| {
            let pb = PathBuf::from(*p);
            pb.exists().then_some(pb)
        })
        .collect()
}

/// Add only the executable's installation directories, never the user's HOME.
pub fn binary_ro(binary: &str) -> std::io::Result<Vec<PathBuf>> {
    let path = Path::new(binary);
    let resolved = if path.components().count() > 1 || path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .map(|dir| dir.join(path))
            .find(|p| p.is_file())
            .ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::NotFound, "harness binary missing")
            })?
    };
    let canonical = std::fs::canonicalize(&resolved)?;
    let home = std::env::var_os("HOME").and_then(|p| std::fs::canonicalize(p).ok());
    let mut ro = Vec::new();
    let mut add = |p: &Path| -> std::io::Result<()> {
        let p = std::fs::canonicalize(p)?;
        if p.parent().is_none() || home.as_ref() == Some(&p) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "refusing broad harness read access",
            ));
        }
        if !ro.contains(&p) {
            ro.push(p);
        }
        Ok(())
    };
    if let Some(parent) = canonical.parent() {
        add(parent)?;
    }
    // npm-style links point into an installation with sibling runtime assets.
    if std::fs::symlink_metadata(&resolved)?
        .file_type()
        .is_symlink()
    {
        if let Some(parent) = canonical.parent().and_then(Path::parent) {
            add(parent)?;
        }
    }
    Ok(ro)
}

/// Rebuild `cmd` as `systemd-run --user --scope … -- pg-jail … -- env -i … <cmd>`.
///
/// `wrap` expects `cmd` already confined by `sandbox_env::confine_harness`
/// (env_clear + allow-list): the harness cleared everything and re-set an
/// allow-list, so `cmd.get_envs()` entries with a value are the child's
/// *complete* environment and `env -i` can enumerate it exactly. `Command`
/// cannot report a prior `env_clear`, so nothing is copied onto the outer
/// launcher — it must inherit the caller's environment to reach the user
/// manager — and the inner `/usr/bin/env -i K=V…` is what strips it back down
/// for the agent. Stdio is left default for the caller; `current_dir` is
/// carried over so relative writes stay in the worktree.
pub fn wrap(cmd: Command, limits: &ScopeLimits, jail: &Jail) -> Command {
    let mut out = Command::new("systemd-run");
    out.arg("--user")
        .arg("--scope")
        .arg("--quiet")
        .arg("--collect")
        .arg("-p")
        .arg(format!("MemoryMax={}", limits.memory_max))
        .arg("-p")
        .arg("MemorySwapMax=0")
        .arg("-p")
        .arg(format!("TasksMax={}", limits.tasks_max))
        .arg("--")
        .arg(jail.bin);
    for p in &jail.rw {
        out.arg("--rw").arg(p);
    }
    for p in &jail.ro {
        out.arg("--ro").arg(p);
    }
    // Let the helper grant only its own proc entries, not the host's process tree.
    out.arg("--proc-self")
        .arg("--")
        .arg("/usr/bin/env")
        .arg("-i");
    for (k, v) in cmd.get_envs().filter_map(|(k, v)| v.map(|v| (k, v))) {
        let mut kv = k.to_os_string();
        kv.push("=");
        kv.push(v);
        out.arg(kv);
    }
    out.arg(cmd.get_program());
    out.args(cmd.get_args());
    if let Some(dir) = cmd.get_current_dir() {
        out.current_dir(dir);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(cmd: &Command) -> Vec<String> {
        std::iter::once(cmd.get_program())
            .chain(cmd.get_args())
            .map(|a| a.to_string_lossy().into_owned())
            .collect()
    }

    fn sample() -> (PathBuf, Command) {
        let mut cmd = Command::new("/usr/bin/opencode");
        cmd.arg("run")
            .arg("do things")
            .env("A", "1")
            .env("B", "two")
            .current_dir("/work/w");
        (PathBuf::from("/opt/pg-jail/pg-jail"), cmd)
    }

    fn sample_jail(bin: &Path) -> Jail<'_> {
        Jail {
            bin,
            rw: vec![PathBuf::from("/work")],
            ro: vec![PathBuf::from("/usr"), PathBuf::from("/etc")],
        }
    }

    #[test]
    fn argv_layout_is_exact() {
        let (bin, cmd) = sample();
        let wrapped = wrap(cmd, &ScopeLimits::default(), &sample_jail(&bin));
        let expected: Vec<String> = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "-p",
            "MemoryMax=2G",
            "-p",
            "MemorySwapMax=0",
            "-p",
            "TasksMax=256",
            "--",
            "/opt/pg-jail/pg-jail",
            "--rw",
            "/work",
            "--ro",
            "/usr",
            "--ro",
            "/etc",
            "--proc-self",
            "--",
            "/usr/bin/env",
            "-i",
            "A=1",
            "B=two",
            "/usr/bin/opencode",
            "run",
            "do things",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect();
        assert_eq!(argv(&wrapped), expected);
    }

    #[test]
    fn env_and_cwd_are_preserved_for_the_child() {
        let (bin, cmd) = sample();
        let wrapped = wrap(cmd, &ScopeLimits::default(), &sample_jail(&bin));
        assert_eq!(wrapped.get_current_dir(), Some(Path::new("/work/w")));
        // The launcher must inherit, never receive a copied environment.
        assert!(wrapped.get_envs().next().is_none());
        let a = argv(&wrapped);
        assert!(a.iter().any(|x| x == "A=1"));
        assert!(a.iter().any(|x| x == "B=two"));
        assert_eq!(a.last().map(String::as_str), Some("do things"));
    }

    #[test]
    fn vars_the_harness_never_set_stay_out_of_argv() {
        std::env::set_var("PG_SECRET_PROBE", "sekrit");
        let (bin, mut cmd) = sample();
        cmd.env_remove("GONE");
        let wrapped = wrap(cmd, &ScopeLimits::default(), &sample_jail(&bin));
        let a = argv(&wrapped);
        assert!(a
            .iter()
            .all(|x| !x.contains("sekrit") && !x.starts_with("PG_SECRET_PROBE=")));
        // env_remove entries are removals, not assignments: never emitted.
        assert!(a.iter().all(|x| !x.starts_with("GONE=")));
        std::env::remove_var("PG_SECRET_PROBE");
    }

    #[test]
    fn swap_and_tasks_limits_are_pinned() {
        let (bin, cmd) = sample();
        let wrapped = wrap(cmd, &ScopeLimits::default(), &sample_jail(&bin));
        let a = argv(&wrapped);
        assert!(a
            .windows(2)
            .any(|w| w[0] == "-p" && w[1] == "MemorySwapMax=0"));
        assert!(a.iter().any(|x| x.starts_with("TasksMax=")));
        assert!(a.iter().any(|x| x.starts_with("MemoryMax=")));
    }

    #[test]
    fn system_ro_only_returns_existing_paths() {
        let ro = system_ro();
        assert!(!ro.is_empty());
        assert!(!ro.contains(&PathBuf::from("/proc")));
        const ALLOWED: &[&str] = &[
            "/usr",
            "/usr/local",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc",
            "/dev",
            "/opt",
            "/run/systemd/resolve",
        ];
        for p in &ro {
            assert!(p.exists(), "{p:?} missing");
            assert!(
                ALLOWED.contains(&p.to_str().unwrap()),
                "{p:?} not a candidate"
            );
        }
    }

    #[test]
    fn wrapped_system_ro_uses_proc_self() {
        let (bin, cmd) = sample();
        let jail = Jail {
            bin: &bin,
            rw: vec![PathBuf::from("/work")],
            ro: system_ro(),
        };
        let wrapped = wrap(cmd, &ScopeLimits::default(), &jail);
        let a = argv(&wrapped);
        assert_eq!(a.iter().filter(|arg| *arg == "--proc-self").count(), 1);
        let helper_start = a
            .iter()
            .position(|arg| arg == bin.to_str().unwrap())
            .unwrap();
        let helper_args: Vec<_> = a[helper_start + 1..]
            .iter()
            .take_while(|arg| arg.as_str() != "--")
            .collect();
        assert!(helper_args.iter().any(|arg| arg.as_str() == "--proc-self"));
        assert!(!a
            .windows(2)
            .any(|args| args[0] == "--ro" && args[1] == "/proc"));
    }

    #[test]
    fn default_limits_are_2g_and_256_tasks() {
        let d = ScopeLimits::default();
        assert_eq!(d.memory_max, "2G");
        assert_eq!(d.tasks_max, 256);
    }

    #[test]
    fn check_scope_carries_attempt_limits() {
        let (bin, _) = sample();
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "echo check"]);
        let limits = ScopeLimits {
            memory_max: "768M".into(),
            tasks_max: 17,
        };
        let a = argv(&wrap(cmd, &limits, &sample_jail(&bin)));
        assert!(a.contains(&"MemoryMax=768M".into()));
        assert!(a.contains(&"TasksMax=17".into()));
    }

    #[cfg(unix)]
    #[test]
    fn binary_under_home_grants_install_not_home() {
        let home = tempfile::tempdir().unwrap();
        let install = home.path().join(".local/share/tool/version");
        std::fs::create_dir_all(install.join("bin")).unwrap();
        let binary = install.join("bin/tool");
        std::fs::write(&binary, "binary").unwrap();
        let link = home.path().join(".local/bin/tool");
        std::fs::create_dir_all(link.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(&binary, &link).unwrap();
        let ro = binary_ro(link.to_str().unwrap()).unwrap();
        assert!(ro.contains(&std::fs::canonicalize(install.join("bin")).unwrap()));
        assert!(ro.contains(&std::fs::canonicalize(&install).unwrap()));
        assert!(!ro.contains(&std::fs::canonicalize(home.path()).unwrap()));
    }
}
