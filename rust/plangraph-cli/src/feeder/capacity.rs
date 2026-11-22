use super::{Env, HostFacts, Policy};
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

pub(super) fn apply(
    facts: &mut HostFacts,
    policy: &mut Policy,
    cpus: usize,
    load: f64,
) -> Result<(), String> {
    if facts.host.is_empty() || cpus == 0 || !load.is_finite() || load < 0.0 {
        return Err("invalid live host capacity".into());
    }
    let free_cores = (cpus as f64 - load).max(0.0).floor() as usize;
    policy.max_writers.insert(
        facts.host.clone(),
        facts.running_writers.saturating_add(free_cores),
    );
    facts.free_ram_bytes = facts.free_ram_bytes.saturating_sub(policy.ram_floor_bytes);
    Ok(())
}
