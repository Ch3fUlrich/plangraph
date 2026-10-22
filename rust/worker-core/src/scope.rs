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
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc",
        "/proc",
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
    out.arg("--").arg("/usr/bin/env").arg("-i");
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
        const ALLOWED: &[&str] = &[
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc",
            "/proc",
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
    fn default_limits_are_2g_and_256_tasks() {
        let d = ScopeLimits::default();
        assert_eq!(d.memory_max, "2G");
        assert_eq!(d.tasks_max, 256);
    }
}
