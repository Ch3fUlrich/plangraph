use pg_worker_core::{
    host_launch, host_overflow,
    hosts::HostRegistry,
    package_placement::{self, Policy},
    run::{record::new_run_id, record_io},
};
use std::{
    env, fs, io,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};
fn run() -> io::Result<u8> {
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("HOME required"))?;
    let mut registry = home.join(".config/plangraph/hosts.json");
    let mut state = home.join(".local/state/plangraph");
    let mut policy = Policy::default();
    let mut spawn_request = None;
    let mut order: Option<String> = None;
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        if flag == "--allow-workstation" {
            policy.allow_workstation = true;
            continue;
        }
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        match flag.as_str() {
            "--registry" => registry = value.into(),
            "--state" => state = value.into(),
            "--spawn" => spawn_request = Some(PathBuf::from(value)),
            "--order" => order = Some(value),
            "--exclude-host" => {
                policy.excluded_hosts.insert(value);
            }
            "--reservation-mb" => {
                policy.reservation_mb = value.parse().map_err(io::Error::other)?
            }
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    let registry = HostRegistry::load(&registry).map_err(io::Error::other)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs();
    let spawn_request: Option<serde_json::Value> = spawn_request
        .map(|path| -> io::Result<_> {
            let request: serde_json::Value = serde_json::from_slice(&fs::read(path)?)?;
            if !request.is_object()
                || !request["card"].is_object()
                || !request["prompt"].is_string()
            {
                return Err(io::Error::other(
                    "spawn request requires card object and prompt string",
                ));
            }
            Ok(request)
        })
        .transpose()?;
    let role = serde_json::from_value(
        spawn_request
            .as_ref()
            .and_then(|r| r.get("role"))
            .cloned()
            .unwrap_or_else(|| "implement".into()),
    )
    .map_err(io::Error::other)?;
    policy.apply_registry(&registry, role);
    let mut hosts = package_placement::load_for_role(&registry, &state, now, role);
    if let Some(order) = order {
        let ids: Vec<_> = order.split(',').collect();
        let unique: std::collections::BTreeSet<_> = ids.iter().copied().collect();
        if unique.len() != ids.len()
            || ids.len() != hosts.len()
            || hosts.iter().any(|h| !unique.contains(h.id.as_str()))
        {
            return Err(io::Error::other(
                "--order must name each eligible host exactly once",
            ));
        }
        hosts.sort_by_key(|h| ids.iter().position(|id| *id == h.id).unwrap());
    }
    if let Some(mut request) = spawn_request {
        request["wait_seconds"] = 0.into();
        let outcome = host_overflow::spawn(&hosts, &policy, now, |host| {
            host_launch::launch(&registry, host, &request)
        });
        let bytes = serde_json::to_vec(&outcome)?;
        record_io::atomic_write_private(
            &state
                .join("overflow")
                .join(format!("{}.json", new_run_id())),
            &bytes,
        )?;
        println!("{}", String::from_utf8_lossy(&bytes));
        return Ok(if outcome.queued {
            5
        } else if outcome.error.is_some() {
            1
        } else {
            0
        });
    }
    let choice = package_placement::choose(&hosts, &policy, now);
    println!("{}", serde_json::to_string_pretty(&choice)?);
    Ok(if choice.host.is_some() { 0 } else { 5 })
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(code) => code,
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
    .into()
}
