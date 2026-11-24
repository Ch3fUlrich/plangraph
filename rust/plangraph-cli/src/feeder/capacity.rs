use super::{writer_limit, Env, HostFacts, Policy};
use pg_worker_core::{disk_runtime, host_probe};
use std::path::Path;

pub(super) fn sample(
    host: &str,
    disk_root: &Path,
    env: &Env,
) -> Result<(HostFacts, usize, f64), String> {
    let metrics = host_probe::probe(host).map_err(|e| format!("capacity probe: {e}"))?;
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
    Ok((
        facts,
        metrics.cpus,
        metrics.load1.ok_or("capacity load unavailable")?,
    ))
}

/// The writer budget of one live sample: the slots free RAM allows, the pacing that load adds,
/// and the numbers behind both, so the decision line shows why a host admits or refuses.
#[derive(Debug, PartialEq)]
pub(super) struct Budget {
    pub slots_by_ram: usize,
    pub new_writers_allowed: usize,
    pub load_paced: bool,
    pub limit: usize,
}

/// Hosts admit by free RAM (D-5122): new writer slots = (free RAM - floor) / RAM per writer.
/// Load never refuses; a host whose load1 is at or above its cpu count only starts one new
/// writer per tick, so builds already running get room before the next one starts.
pub(super) fn apply(
    facts: &mut HostFacts,
    policy: &mut Policy,
    cpus: usize,
    load: f64,
) -> Result<Budget, String> {
    if facts.host.is_empty() || cpus == 0 || !load.is_finite() || load < 0.0 {
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
    let load_paced = load >= cpus as f64;
    let new_writers_allowed = if load_paced {
        slots_by_ram.min(1)
    } else {
        slots_by_ram
    };
    let limit = facts.running_writers.saturating_add(new_writers_allowed);
    // Only a host the policy lists runs writers; an unlisted host keeps refusing by name.
    if writer_limit(&facts.host, policy).is_some() {
        policy.max_writers.insert(facts.host.clone(), limit);
    }
    Ok(Budget {
        slots_by_ram,
        new_writers_allowed,
        load_paced,
        limit,
    })
}
