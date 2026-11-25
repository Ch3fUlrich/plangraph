use super::{writer_limit, Env, HostFacts, Policy};
use pg_worker_core::{disk_runtime, host_probe};
use std::path::Path;

/// What a live sample says about CPU, beside the host facts: the effective cpu count, the
/// one-minute load when the host reports one, and whether the kernel keeps a CPU pressure
/// (PSI) file, which then owns CPU saturation instead of load1.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(super) struct Cpu {
    pub cpus: usize,
    pub load1: Option<f64>,
    pub psi: bool,
}

/// Only a missing `pressure/cpu` means "no PSI": a present file that is malformed, or that
/// cannot be opened (permission denied, a non-directory parent), still belongs to a PSI
/// kernel, so load must not pace in its place.
fn psi_present<T>(opened: std::io::Result<T>) -> bool {
    !matches!(opened, Err(e) if e.kind() == std::io::ErrorKind::NotFound)
}
fn has_psi(proc_root: &Path) -> bool {
    psi_present(std::fs::File::open(proc_root.join("pressure/cpu")))
}

pub(super) fn sample(
    host: &str,
    disk_root: &Path,
    env: &Env,
    proc_root: &Path,
) -> Result<(HostFacts, Cpu), String> {
    let metrics = host_probe::probe_load_optional(host, proc_root)
        .map_err(|e| format!("capacity probe: {e}"))?;
    let disk = |path: &Path| {
        disk_runtime::disk_usage(path).map_err(|e| format!("capacity disk probe: {e}"))
    };
    let mut free_disk_bytes = disk(disk_root)?;
    if let Some(target) = env
        .get("CARGO_TARGET_DIR")
        .map(Path::new)
        .filter(|p| p.exists())
    {
        free_disk_bytes = free_disk_bytes.min(disk(target)?);
    }
    let facts = HostFacts {
        host: host.to_owned(),
        free_ram_bytes: metrics.mem_available_mb.saturating_mul(1024 * 1024),
        free_disk_bytes,
        quota_remaining_percent: None,
        observed_at: (metrics.ts_ms / 1000) as i64,
        running_writers: 0,
        kill_switch: false,
        now: 0,
    };
    let cpu = Cpu {
        cpus: metrics.cpus,
        load1: metrics.load1,
        psi: has_psi(proc_root),
    };
    Ok((facts, cpu))
}

/// The writer budget of one live sample: the slots free RAM allows, the pacing that load adds,
/// and the numbers behind both, so the decision line shows why a host admits or refuses.
/// `limit` is `None` for a host the policy does not list: it has RAM slots but no writers.
#[derive(Debug, PartialEq)]
pub(super) struct Budget {
    pub slots_by_ram: usize,
    pub new_writers_allowed: usize,
    pub load_paced: bool,
    pub limit: Option<usize>,
}

/// Hosts admit by free RAM (D-5122): new writer slots = (free RAM - floor) / RAM per writer.
/// Load never refuses; a host without a PSI file whose load1 is at or above its cpu count
/// only starts one new writer per tick, so builds already running get room first.
/// The limit of a listed host is written into `policy`; an unlisted host gets none.
pub(super) fn apply(
    facts: &mut HostFacts,
    policy: &mut Policy,
    cpu: Cpu,
) -> Result<Budget, String> {
    if facts.host.is_empty()
        || cpu.cpus == 0
        || cpu
            .load1
            .is_some_and(|load| !load.is_finite() || load < 0.0)
    {
        return Err("invalid live host capacity".into());
    }
    facts.free_ram_bytes = facts.free_ram_bytes.saturating_sub(policy.ram_floor_bytes);
    let slots_by_ram = usize::try_from(
        facts
            .free_ram_bytes
            .checked_div(policy.ram_per_run_bytes)
            .unwrap_or(0),
    )
    .unwrap_or(usize::MAX);
    let load_paced = !cpu.psi && cpu.load1.is_some_and(|load| load >= cpu.cpus as f64);
    let listed = writer_limit(&facts.host, policy).is_some();
    let new_writers_allowed = match (listed, load_paced) {
        (false, _) => 0,
        (true, true) => slots_by_ram.min(1),
        (true, false) => slots_by_ram,
    };
    let limit = listed.then(|| facts.running_writers.saturating_add(new_writers_allowed));
    if let Some(limit) = limit {
        policy.max_writers.insert(facts.host.clone(), limit);
    }
    Ok(Budget {
        slots_by_ram,
        new_writers_allowed,
        load_paced,
        limit,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Error, ErrorKind};

    #[test]
    fn only_a_missing_pressure_file_means_no_psi() {
        assert!(!psi_present::<()>(Err(ErrorKind::NotFound.into())));
        for kind in [
            ErrorKind::PermissionDenied,
            ErrorKind::InvalidData,
            ErrorKind::NotADirectory,
            ErrorKind::Other,
        ] {
            assert!(psi_present::<()>(Err(Error::from(kind))), "{kind:?}");
        }
        assert!(psi_present(Ok(())));
    }
}
