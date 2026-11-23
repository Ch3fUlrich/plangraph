use super::{
    paths::{read_text, shell_quote, var},
    process::capture,
    Request,
};
use serde_json::{json, Value};
use std::{
    path::Path,
    process::{Command, Output},
    time::Duration,
};

const WINDOWS_FREE: &str = "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory";

pub(super) struct Placement {
    local: String,
    hostname: String,
    hosts: Vec<Host>,
    worker: bool,
}

#[derive(Clone)]
pub(super) struct Host(Value);

impl Host {
    pub fn name(&self) -> &str {
        self.0["name"].as_str().unwrap_or_default()
    }
    fn ssh(&self) -> Option<&str> {
        self.0["ssh"].as_str()
    }
    fn enabled(&self) -> bool {
        self.0["enabled"].as_bool().unwrap_or(true)
    }
    fn last_resort(&self) -> bool {
        self.0["last_resort"].as_bool().unwrap_or(false)
    }

    fn ssh_command(&self, connect: u64) -> std::io::Result<Command> {
        let destination = self
            .ssh()
            .filter(|s| !s.is_empty() && !s.starts_with('-'))
            .ok_or_else(|| std::io::Error::other("invalid SSH destination"))?;
        let mut cmd = Command::new("ssh");
        cmd.env_remove("FLEET_FORWARDED");
        cmd.args([
            "-o",
            "BatchMode=yes",
            "-o",
            &format!("ConnectTimeout={connect}"),
            destination,
        ]);
        Ok(cmd)
    }

    pub fn bash(&self, command: &str) -> std::io::Result<Command> {
        let mut ssh = self.ssh_command(5)?;
        ssh.arg(format!("bash -lc {}", shell_quote(command)));
        Ok(ssh)
    }

    fn floor(&self) -> Result<u64, &'static str> {
        let floor = &self.0["memory_floor_gib"];
        if floor.is_null() {
            return Err("no memory floor");
        }
        let gib = floor
            .as_f64()
            .or_else(|| floor.as_str()?.trim().parse().ok())
            .or_else(|| floor.as_bool().map(|n| f64::from(u8::from(n))));
        let mb = gib.ok_or("invalid memory floor")? * 1024.0;
        if !mb.is_finite() || mb < 0.0 {
            return Err("invalid memory floor");
        }
        Ok(mb.ceil() as u64)
    }
}

impl Placement {
    pub fn new(request: &Request, policy: Option<&Value>) -> Self {
        let worker_role = (!request.args.respawn).then_some(request.args.role.as_str());
        Self::detect(policy, worker_role)
    }

    fn detect(policy: Option<&Value>, worker_role: Option<&str>) -> Self {
        let hostname = fs_hostname();
        let local = std::env::var("FLEET_LOCAL_HOST").unwrap_or_else(|_| hostname.clone());
        Self::with_hostname(policy, worker_role, local, hostname)
    }

    pub fn with_hostname(
        policy: Option<&Value>,
        worker_role: Option<&str>,
        local: String,
        hostname: String,
    ) -> Self {
        let hosts = policy
            .and_then(|p| p["hosts"].as_array())
            .into_iter()
            .flatten()
            .filter(|v| v["name"].is_string())
            .cloned()
            .map(Host)
            .collect();
        let worker = worker_role.is_some_and(|role| {
            policy
                .and_then(|p| p["worker_roles"].as_array())
                .is_some_and(|roles| roles.iter().any(|candidate| candidate == role))
        });
        Self {
            local,
            hostname,
            hosts,
            worker,
        }
    }

    pub fn fallback(
        &self,
        explicit: Option<&str>,
        mut available: impl FnMut(Option<&Host>) -> Result<u64, ()>,
    ) -> Result<(Host, Vec<String>), String> {
        if let Some(name) = explicit {
            return self.named(name).map(|host| (host, Vec::new()));
        }
        if self.hosts.is_empty() {
            return Ok((
                Host(json!({"name": self.local, "ssh": self.local})),
                Vec::new(),
            ));
        }
        let pool: Vec<_> = self.hosts.iter().filter(|host| host.enabled()).collect();
        if pool.is_empty() {
            return Err("no enabled host (no queue)".into());
        }
        let index = pool.iter().position(|host| self.local(host)).unwrap_or(0);
        let here = pool[index];
        let floor = here.0["memory_floor_gib"]
            .as_f64()
            .filter(|n| *n != 0.0)
            .unwrap_or(2.0);
        if available(None).is_ok_and(|bytes| bytes as f64 >= floor * (1u64 << 30) as f64) {
            return Ok((here.clone(), Vec::new()));
        }
        let mut notices = Vec::new();
        for host in &pool[index + 1..] {
            let Some(required) = host.0["memory_floor_gib"].as_f64() else {
                continue;
            };
            match available(Some(host)) {
                Ok(bytes) if bytes as f64 >= required * (1u64 << 30) as f64 => {
                    notices.push(format!(
                        "fleet-launch: {} below {floor} GiB floor; selecting {}",
                        here.name(),
                        host.name()
                    ));
                    return Ok(((*host).clone(), notices));
                }
                Err(()) => notices.push(format!("fleet-launch: host {} unreachable", host.name())),
                Ok(_) => {}
            }
        }
        notices.push(format!(
            "fleet-launch: ALERT no host above floor; launching here on {} (no queue)",
            here.name()
        ));
        Ok((here.clone(), notices))
    }

    pub fn local(&self, host: &Host) -> bool {
        host.name() == self.local
            || (host.name() == ["coding", "vm"].join(".")
                && matches!(self.local.split('.').next(), Some("coding" | "code")))
    }

    pub fn policy_host<'a>(&self, host: &'a Host) -> Option<&'a str> {
        (!self.hosts.is_empty()).then(|| host.name())
    }

    pub fn choose(&self, request: &Request, forwarded: bool) -> Result<Host, String> {
        let mut explicit = request
            .args
            .option("host")
            .filter(|s| !s.is_empty())
            .or_else(|| request.spec["host"].as_str().filter(|s| !s.is_empty()));
        if explicit.is_none() && request.args.respawn && !self.hosts.is_empty() {
            explicit = Some(self.hosts.iter().find(|h| self.local(h)).ok_or(
                "respawn refused: this host is not in the placement policy and the saved spec names no host (set FLEET_LOCAL_HOST to its policy name)"
            )?.name());
        }
        let host = self.select(explicit)?;
        if self.worker
            && explicit.is_some()
            && host.last_resort()
            && !forwarded
            && var("FLEET_LAST_RESORT_REASON").is_none()
        {
            if let Ok(other) = self.select(None) {
                if !other.last_resort() {
                    let role = &request.args.role;
                    let other = other.name();
                    let name = host.name();
                    return Err(format!("refused: {role} is a worker role and {other} has room; {name} is the last resort. Launch with --host {other} (worktree on that host), or set FLEET_LAST_RESORT_REASON=<why it must run here>"));
                }
            }
        }
        Ok(host)
    }

    fn select(&self, explicit: Option<&str>) -> Result<Host, String> {
        let mut pool = if explicit.is_some() || self.hosts.is_empty() {
            let (host, _) = self.fallback(explicit, |_| {
                unreachable!("no memory read for a named or empty policy")
            })?;
            vec![host]
        } else {
            self.hosts.clone()
        };
        let compare = self.worker && explicit.is_none() && pool.iter().any(Host::last_resort);
        if compare {
            pool.sort_by_key(Host::last_resort);
        } else if let Some(index) = pool.iter().position(|h| self.local(h)) {
            pool.drain(..index);
        }
        let mut best = None;
        for host in pool {
            if explicit.is_none() && !host.enabled() {
                continue;
            }
            if compare && host.last_resort() && best.is_some() {
                break;
            }
            let name = host.name();
            let floor = match host.floor() {
                Ok(value) => value,
                Err(error) => {
                    eprintln!("skip {name}: {error}");
                    continue;
                }
            };
            let Some(memory) = self.memory(&host, compare) else {
                eprintln!("skip {name}: memory unknown");
                continue;
            };
            if memory < floor {
                eprintln!("skip {name}: MemAvailable {memory} MB < floor {floor} MB");
                if explicit.is_some() {
                    return Err(format!("{name} below memory floor: {memory} MB available, floor {floor} MB (no queue)"));
                }
                continue;
            }
            if !compare || host.last_resort() {
                return Ok(host);
            }
            if best
                .as_ref()
                .is_none_or(|(headroom, _)| memory - floor > *headroom)
            {
                best = Some((memory - floor, host));
            }
        }
        best.map(|(_, host)| host)
            .ok_or_else(|| "no host above memory floor (no queue)".to_owned())
    }

    fn named(&self, name: &str) -> Result<Host, String> {
        if let Some(host) = self.hosts.iter().find(|h| h.name() == name) {
            if host.enabled() || host.0["explicit_enabled"].as_bool() == Some(true) {
                return Ok(host.clone());
            }
            return Err(format!("refused host {name}: disabled"));
        }
        if self.hosts.is_empty() && (name == self.local || name == self.hostname) {
            return Ok(Host(json!({"name": name, "ssh": name})));
        }
        Err(format!("refused host {name}: unknown host"))
    }

    fn memory(&self, host: &Host, compare: bool) -> Option<u64> {
        let local = self.local(host);
        let windows = if host.0["memory_source"] == "windows" && (!local || compare) {
            let mut command = if local {
                let mut command = Command::new("powershell.exe");
                command.env_remove("FLEET_FORWARDED");
                command.args(["-NoProfile", "-Command", WINDOWS_FREE]);
                command
            } else {
                let mut command = host.ssh_command(5).ok()?;
                command.arg(format!("powershell -NoProfile -Command \"{WINDOWS_FREE}\""));
                command
            };
            let output = run_probe(&mut command, 10)?.replace('\0', "");
            Some(output.trim().parse::<u64>().ok()? / 1024)
        } else {
            None
        };
        let info = if local {
            read_text(Path::new("/proc/meminfo")).ok()?
        } else {
            let mut command = if host.0["shell"] == "bash" {
                host.bash("cat /proc/meminfo").ok()?
            } else {
                let mut command = host.ssh_command(3).ok()?;
                command.arg("cat /proc/meminfo");
                command
            };
            run_probe(&mut command, 5)?
        };
        let memory = meminfo(&info)?;
        Some(windows.map_or(memory, |windows| memory.min(windows)))
    }
}

pub(super) fn legacy_host(policy_path: &Path, explicit: Option<&str>) -> Result<String, String> {
    let path = std::env::var_os("FLEET_HOST_POLICY").map(std::path::PathBuf::from);
    let policy = super::policy::load(path.as_deref().unwrap_or(policy_path));
    let placement = Placement::detect(policy.as_ref(), None);
    let (host, messages) = placement.fallback(explicit, |remote| {
        let text = if let Some(host) = remote.filter(|host| !placement.local(host)) {
            run_probe(&mut host.bash("cat /proc/meminfo").map_err(|_| ())?, 5).ok_or(())?
        } else {
            let override_path = std::env::var_os("FLEET_MEMINFO");
            read_text(
                override_path
                    .as_deref()
                    .map_or(Path::new("/proc/meminfo"), Path::new),
            )
            .map_err(|_| ())?
        };
        text.lines()
            .find(|line| line.starts_with("MemAvailable:"))
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|kb| kb.parse::<u64>().ok())
            .and_then(|kb| kb.checked_mul(1024))
            .ok_or(())
    })?;
    for message in messages {
        eprintln!("{message}");
    }
    Ok(host.name().to_owned())
}

fn meminfo(info: &str) -> Option<u64> {
    for line in info.lines() {
        let mut fields = line.split_whitespace();
        if fields.next() == Some("MemAvailable:") {
            let kb = fields.next()?.parse::<u64>().ok()?;
            return (fields.next() == Some("kB") && fields.next().is_none()).then_some(kb / 1024);
        }
    }
    None
}

fn run_probe(command: &mut Command, seconds: u64) -> Option<String> {
    let Output { status, stdout, .. } =
        capture(command, None, Duration::from_secs(seconds)).ok()?;
    status
        .success()
        .then(|| String::from_utf8(stdout).ok())
        .flatten()
}

pub(super) fn fs_hostname() -> String {
    let mut name = [0u8; 256];
    // SAFETY: name is writable for its full length.
    if unsafe { libc::gethostname(name.as_mut_ptr().cast(), name.len()) } != 0 {
        return String::new();
    }
    String::from_utf8_lossy(name.split(|b| *b == 0).next().unwrap_or_default()).into_owned()
}
