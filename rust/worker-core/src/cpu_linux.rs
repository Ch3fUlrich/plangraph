//! Accounting for a watchdog-owned Linux run group, never card-provided identity.
use crate::cpu::CpuSample;
use std::{collections::HashMap, fs, io, path::PathBuf};

#[cfg(test)]
#[path = "cpu_linux_tests.rs"]
mod tests;

pub struct LinuxSource {
    proc_root: PathBuf,
    cgroup_stat: Option<PathBuf>,
    pgid: u32,
    ticks: u64,
    cgroup: Option<bool>,
    members: HashMap<(u32, u64), u64>,
    total_ticks: u64,
}

impl LinuxSource {
    /// Paths and pgid must come from the owned spawn, not untrusted card data.
    /// The cgroup path, when supplied, must be a validated run-scope cpu.stat.
    pub fn new(proc_root: PathBuf, cgroup_stat: Option<PathBuf>, pgid: u32) -> io::Result<Self> {
        if pgid == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "zero process group",
            ));
        }
        // SAFETY: sysconf takes no pointers and has no side effects on ownership.
        let ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
        let ticks = u64::try_from(ticks)
            .ok()
            .filter(|n| *n > 0)
            .ok_or_else(|| io::Error::other("clock ticks unavailable"))?;
        Ok(Self {
            proc_root,
            cgroup_stat,
            pgid,
            ticks,
            cgroup: None,
            members: HashMap::new(),
            total_ticks: 0,
        })
    }

    pub fn read(&mut self) -> io::Result<CpuSample> {
        let cgroup = self
            .cgroup_stat
            .as_ref()
            .and_then(|path| fs::read_to_string(path).ok())
            .and_then(|text| usage(&text));
        let using_cgroup = cgroup.is_some();
        let changed = self
            .cgroup
            .replace(using_cgroup)
            .is_some_and(|old| old != using_cgroup);
        if changed {
            self.members.clear();
            self.total_ticks = 0;
        }
        let total_ms = if let Some(total) = cgroup {
            total / 1000
        } else {
            let mut members = HashMap::new();
            let mut total_ticks = self.total_ticks;
            for entry in fs::read_dir(&self.proc_root)? {
                let entry = entry?;
                let Some(pid) = entry
                    .file_name()
                    .to_str()
                    .and_then(|s| s.parse::<u32>().ok())
                else {
                    continue;
                };
                let Ok(text) = fs::read_to_string(entry.path().join("stat")) else {
                    continue;
                };
                let Some((group, start, ticks)) = stat(&text, pid) else {
                    continue;
                };
                if group != self.pgid {
                    continue;
                }
                let key = (pid, start);
                if let Some(old) = self.members.get(&key) {
                    total_ticks = total_ticks.saturating_add(ticks.saturating_sub(*old));
                }
                members.insert(key, ticks);
            }
            self.members = members;
            self.total_ticks = total_ticks;
            u64::try_from(u128::from(self.total_ticks) * 1000 / u128::from(self.ticks))
                .map_err(|_| io::Error::other("CPU milliseconds overflow"))?
        };
        if changed {
            // An unavailable sample resets Sampler without adding source metadata to Signal.
            return Err(io::Error::other("CPU accounting source changed"));
        }
        Ok(CpuSample { total_ms })
    }
}

fn usage(text: &str) -> Option<u64> {
    let mut result = None;
    for line in text.lines() {
        let mut fields = line.split_whitespace();
        if fields.next() == Some("usage_usec") {
            if result.is_some() {
                return None;
            }
            result = Some(fields.next()?.parse().ok()?);
            if fields.next().is_some() {
                return None;
            }
        }
    }
    result
}

fn stat(text: &str, pid: u32) -> Option<(u32, u64, u64)> {
    let (head, tail) = text.rsplit_once(") ")?;
    let (id, _) = head.split_once(" (")?;
    if id.parse::<u32>().ok()? != pid {
        return None;
    }
    let fields: Vec<_> = tail.split_whitespace().collect();
    let group = fields.get(2)?.parse().ok()?;
    let user: u64 = fields.get(11)?.parse().ok()?;
    let system: u64 = fields.get(12)?.parse().ok()?;
    let start = fields.get(19)?.parse().ok()?;
    Some((group, start, user.saturating_add(system)))
}
