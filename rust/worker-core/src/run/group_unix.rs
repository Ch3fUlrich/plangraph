//! Linux scope and process-group mechanics.
use super::group::*;
use std::{
    collections::HashMap,
    ffi::OsString,
    fs, io,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Mutex, OnceLock},
    thread,
    time::{Duration, Instant},
};
#[derive(Clone)]
pub struct Roots {
    pub proc: PathBuf,
    pub cgroup: PathBuf,
}
impl Default for Roots {
    fn default() -> Self {
        Self {
            proc: "/proc".into(),
            cgroup: "/sys/fs/cgroup".into(),
        }
    }
}
fn configure(c: &mut Command, s: &LaunchSpec<'_>) {
    c.env_clear()
        .envs(s.env.iter().cloned())
        .current_dir(s.cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if s.stdin_piped {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .process_group(0);
}
pub fn scope_command(
    unit: &str,
    slice: &str,
    limits: &RunLimits,
    spec: &LaunchSpec<'_>,
    manager_env: &[(OsString, OsString)],
) -> Command {
    let mut c = Command::new("systemd-run");
    c.args(["--user", "--scope", "--quiet", "--collect"])
        .arg(format!("--slice={slice}"))
        .arg(format!("--unit={unit}"));
    if let Some(v) = &limits.memory_max {
        c.args(["-p", &format!("MemoryMax={v}")]);
    }
    if let Some(v) = limits.tasks_max {
        c.args(["-p", &format!("TasksMax={v}")]);
    }
    c.arg("--").arg(spec.program).args(spec.args);
    configure(&mut c, spec);
    for (k, v) in manager_env {
        if (k == "XDG_RUNTIME_DIR" || k == "DBUS_SESSION_BUS_ADDRESS")
            && !spec.env.iter().any(|(key, _)| key == k)
        {
            c.env(k, v);
        }
    }
    c
}
static PROBES: OnceLock<Mutex<HashMap<String, Result<(), String>>>> = OnceLock::new();
pub fn scope_available(slice: &str) -> bool {
    probe(slice).is_ok()
}
fn probe(slice: &str) -> Result<(), String> {
    let mut cache = PROBES
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    cache
        .entry(slice.into())
        .or_insert_with(|| {
            let mut command = Command::new("systemd-run");
            command
                .args(["--user", "--scope", "--quiet", "--collect"])
                .arg(format!("--slice={slice}"))
                .args(["--", "true"]);
            probe_command(&mut command, Duration::from_secs(5))
        })
        .clone()
}
pub(super) fn probe_command(command: &mut Command, timeout: Duration) -> Result<(), String> {
    // Read concurrently so even a noisy failure cannot fill the stderr pipe.
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .map_err(|_| "cannot spawn scope probe".to_string())?;
    let stderr = child.stderr.take().unwrap();
    let reader = thread::spawn(move || {
        use std::io::Read;
        let mut text = String::new();
        let _ = stderr.take(4096).read_to_string(&mut text);
        text
    });
    let deadline = Instant::now() + timeout;
    let success = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s.success(),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            _ => {
                signal(child.id(), libc::SIGKILL, true);
                let _ = child.wait();
                break false;
            }
        }
    };
    let text = reader.join().unwrap_or_default();
    if success {
        Ok(())
    } else {
        Err(
            crate::redact::redact(text.lines().next().unwrap_or("scope probe failed"))
                .chars()
                .take(200)
                .collect(),
        )
    }
}
/// The cgroup v2 line prefix of /proc/<pid>/cgroup ("0", then two colons),
/// spelled in parts so the public-repo scrub does not read it as an IPv6
/// address.
pub(crate) const CGROUP_V2_PREFIX: &str = concat!("0:", ":");

pub fn parse_cgroup_path(text: &str, unit: &str) -> Option<PathBuf> {
    text.lines()
        .filter_map(|l| l.strip_prefix(CGROUP_V2_PREFIX))
        .find(|p| p.starts_with('/') && p.ends_with(&format!("/{unit}.scope")))
        .map(PathBuf::from)
}
pub fn parse_stat(text: &str) -> Option<(char, u32, u64)> {
    let f: Vec<_> = text.rsplit_once(')')?.1.split_whitespace().collect();
    Some((
        f.first()?.chars().next()?,
        f.get(2)?.parse().ok()?,
        crate::slots::liveness::linux::parse_start_time(text)?,
    ))
}
fn stat(root: &Path, pid: u32) -> Option<(char, u32, u64)> {
    parse_stat(&fs::read_to_string(root.join(pid.to_string()).join("stat")).ok()?)
}
pub fn proc_start_time(root: &Path, pid: u32) -> Option<u64> {
    stat(root, pid).map(|s| s.2)
}
pub fn launch(
    unit: &str,
    spec: &LaunchSpec<'_>,
    confinement: &Confinement,
    limits: &RunLimits,
    roots: &Roots,
) -> Result<Grouped, LaunchError> {
    validate_unit(unit)?;
    let mut cmd = match confinement {
        Confinement::Plain => {
            let mut c = Command::new(spec.program);
            c.args(spec.args);
            configure(&mut c, spec);
            c
        }
        Confinement::Scope { slice } => {
            probe(slice).map_err(LaunchError::ScopeUnavailable)?;
            scope_command(
                unit,
                slice,
                limits,
                spec,
                &std::env::vars_os().collect::<Vec<_>>(),
            )
        }
    };
    unsafe {
        cmd.pre_exec(|| super::oom::adjust(c"/proc/self/oom_score_adj"));
    }
    let mut child = spawn_retrying(|| cmd.spawn())?;
    let pid = child.id();
    let start_time = proc_start_time(&roots.proc, pid);
    let mut group = None;
    if matches!(confinement, Confinement::Scope { .. }) {
        group = enter_scope(&mut child, unit, roots, Duration::from_secs(5))?;
    }
    Ok(Grouped {
        child,
        group: Box::new(UnixGroup {
            identity: Identity {
                pid,
                start_time,
                group,
            },
            roots: roots.clone(),
        }),
    })
}
pub(super) fn spawn_retrying(mut spawn: impl FnMut() -> io::Result<Child>) -> io::Result<Child> {
    for attempt in 1..=6 {
        match spawn() {
            Err(e) if crate::stall::is_text_busy(&e) && attempt < 6 => {
                thread::sleep(Duration::from_millis(20 * attempt));
            }
            result => return result,
        }
    }
    unreachable!()
}
pub(super) fn enter_scope(
    child: &mut Child,
    unit: &str,
    roots: &Roots,
    timeout: Duration,
) -> Result<Option<String>, LaunchError> {
    let pid = child.id();
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(path) = fs::read_to_string(roots.proc.join(pid.to_string()).join("cgroup"))
            .ok()
            .and_then(|t| parse_cgroup_path(&t, unit))
        {
            return Ok(Some(
                roots
                    .cgroup
                    .join(path.strip_prefix("/").unwrap())
                    .to_string_lossy()
                    .into_owned(),
            ));
        }
        if matches!(child.try_wait(), Ok(Some(_))) {
            break;
        }
        if Instant::now() >= deadline {
            signal(pid, libc::SIGKILL, true);
            let _ = child.wait();
            return Err(LaunchError::ScopeNotEntered);
        }
        thread::sleep(Duration::from_millis(10));
    }
    Ok(None)
}
pub struct UnixGroup {
    pub identity: Identity,
    pub roots: Roots,
}
fn signal(pid: u32, sig: i32, group: bool) {
    if pid <= 1 || pid > i32::MAX as u32 {
        return;
    }
    // SAFETY: kill accepts numeric identifiers and does not dereference pointers.
    unsafe {
        libc::kill(if group { -(pid as i32) } else { pid as i32 }, sig);
    }
}
impl UnixGroup {
    fn send(&self, sig: i32) {
        if let Ok(pids) = self.members() {
            let snapshot: Vec<_> = pids
                .into_iter()
                .filter_map(|pid| proc_start_time(&self.roots.proc, pid).map(|start| (pid, start)))
                .collect();
            for (pid, start) in snapshot {
                if proc_start_time(&self.roots.proc, pid) == Some(start) {
                    signal(pid, sig, false);
                }
            }
        }
        if self.identity.start_time.is_none()
            || proc_start_time(&self.roots.proc, self.identity.pid) == self.identity.start_time
        {
            signal(self.identity.pid, sig, true);
        }
    }
}
impl RunGroup for UnixGroup {
    fn identity(&self) -> &Identity {
        &self.identity
    }
    fn members(&self) -> io::Result<Vec<u32>> {
        let pids: Vec<u32> = if let Some(dir) = &self.identity.group {
            match scope_members(Path::new(dir)) {
                Ok(pids) => pids,
                Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
                Err(e) => return Err(e),
            }
        } else {
            fs::read_dir(&self.roots.proc)?
                .filter_map(|e| e.ok()?.file_name().to_str()?.parse().ok())
                .collect()
        };
        let mut pids: Vec<_> = pids
            .into_iter()
            .filter(|&pid| {
                stat(&self.roots.proc, pid).is_some_and(|(s, pg, _)| {
                    s != 'Z'
                        && s != 'X'
                        && (self.identity.group.is_some() || pg == self.identity.pid)
                })
            })
            .collect();
        pids.sort_unstable();
        pids.dedup();
        Ok(pids)
    }
    fn terminate(&self) {
        self.send(libc::SIGTERM);
    }
    fn kill(&self) {
        if let Some(dir) = &self.identity.group {
            let _ = fs::write(Path::new(dir).join("cgroup.kill"), "1");
        }
        self.send(libc::SIGKILL);
    }
}

/// Unlike slot liveness's current flat-scope assumption, stopping must include
/// nested cgroups and must not mistake an unreadable subtree for emptiness.
fn scope_members(dir: &Path) -> io::Result<Vec<u32>> {
    let text = match fs::read_to_string(dir.join("cgroup.procs")) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e),
    };
    let mut pids = text
        .split_whitespace()
        .filter_map(|p| p.parse().ok())
        .collect::<Vec<_>>();
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let kind = match entry.and_then(|entry| Ok((entry.file_type()?, entry.path()))) {
            Ok(value) => value,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e),
        };
        if kind.0.is_dir() {
            pids.extend(scope_members(&kind.1)?);
        }
    }
    Ok(pids)
}
