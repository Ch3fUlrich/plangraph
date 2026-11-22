//! Fleet registry polling and delivery through the coordinator's existing watchdog bridge.

use crate::worker_rejoin::{active_run_ids, now, registry, remote};
use pg_worker_core::{
    host_command, host_link,
    hosts::{HostClass, HostEntry, HostRegistry, Transport},
    run::record::valid_run_id,
    worker_host::{Observation, Store},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    env, fs, io,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

fn state() -> Result<PathBuf, String> {
    crate::state::resolve_state(None, |key| env::var_os(key))
}

fn registry_workers(registry: &HostRegistry) -> impl Iterator<Item = &HostEntry> {
    registry.hosts.iter().filter(|host| {
        host.id != registry.self_host().id
            && host.transport == Transport::Ssh
            && matches!(
                host.class,
                HostClass::Worker | HostClass::Restricted | HostClass::Workstation
            )
    })
}

pub(crate) fn maintenance(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let [name, until] = args else {
        return Err("usage: pg-spawnd worker-maintenance HOST END_UNIX_SECONDS".into());
    };
    let registry = registry()?;
    let host = registry.resolve(name).ok_or("unknown worker host")?;
    if !registry_workers(&registry).any(|worker| worker.id == host.id) {
        return Err("host is outside the coordinator's worker monitor".into());
    }
    let until = until.parse().map_err(|_| "invalid maintenance end")?;
    Store::new(&state()?, &host.id)
        .and_then(|store| store.set_maintenance(now().map_err(io::Error::other)?, until))
        .map_err(|error| error.to_string())?;
    writeln!(
        out,
        "{}",
        json!({"host":host.id,"maintenance_until":until,"phase":"rejoining"})
    )
    .map_err(|error| error.to_string())
}

pub(crate) fn inventory(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let [name] = args else {
        return Err("usage: pg-spawnd worker-active-runs HOST".into());
    };
    let registry = registry()?;
    let host = registry.self_host();
    if &host.id != name {
        return Err("run inventory reached a different host".into());
    }
    let state = if host.state_dir.is_empty() {
        state()?
    } else {
        PathBuf::from(&host.state_dir)
    };
    let runs = active_run_ids(&state, &registry, name);
    writeln!(out, "{}", json!({"host":name,"runs":runs})).map_err(|error| error.to_string())
}

fn parse_inventory(host: &str, bytes: &[u8]) -> Option<BTreeSet<String>> {
    let receipt: Value = serde_json::from_slice(bytes).ok()?;
    if receipt["host"] != host {
        return None;
    }
    receipt["runs"]
        .as_array()?
        .iter()
        .map(|id| id.as_str().filter(|id| valid_run_id(id)).map(str::to_owned))
        .collect()
}

fn check(host: &HostEntry, registry: &HostRegistry, state: &Path) -> Result<(Value, bool), String> {
    let route = host.ssh_route().ok_or("worker lacks an SSH route")?;
    let peer = host
        .env
        .get("PG_TAILSCALE_PEER")
        .map(String::as_str)
        .unwrap_or_else(|| {
            route
                .rsplit('@')
                .next()
                .expect("SSH route")
                .trim_matches(['[', ']'])
        });
    let (ssh, tailscale) = std::thread::scope(|scope| {
        let tailscale = scope.spawn(|| host_link::probe_peer(peer));
        let ssh = host_link::probe_ssh(&route);
        (ssh, tailscale.join().expect("tailscale probe worker"))
    });
    let reachable = ssh.map_err(|_| "SSH probe was unavailable")?;
    // Inventory can take longer than a transport check on a host with many records.
    let runs = reachable
        .then(|| {
            let mut command = remote(host).ok()?;
            let bytes = host_command::output(
                command.args(["pg-spawnd", "worker-active-runs", &host.id]),
                Duration::from_secs(7),
            )
            .ok()?;
            parse_inventory(&host.id, &bytes)
        })
        .flatten();
    let inventory_available = runs.is_some();
    let tailscale = tailscale.ok();
    let (phase, rejoin) = Store::new(state, &host.id)
        .and_then(|store| {
            store.observe_liveness(Observation {
                now: now().map_err(io::Error::other)?,
                ssh: reachable,
                tailscale,
                remote_runs: runs,
                placed_runs: if reachable {
                    BTreeSet::new()
                } else {
                    active_run_ids(state, registry, &host.id)
                },
            })
        })
        .map_err(|error| error.to_string())?;
    Ok((
        json!({"host":host.id,"ssh":reachable,"tailscale":tailscale,
        "inventory_available":inventory_available,"phase":phase,"rejoin_required":rejoin}),
        rejoin,
    ))
}

fn dispatch(host: &str) -> Result<(), String> {
    host_command::output(
        Command::new("systemctl").args([
            "--user",
            "start",
            "--no-block",
            &format!("pg-spawnd-rejoin@{host}.service"),
        ]),
        Duration::from_secs(5),
    )
    .map(drop)
    .map_err(|_| "could not queue worker rejoin unit".into())
}

fn deliver(state: &Path) -> Result<(), String> {
    let outbox = state.join("watchdog/outbox");
    let entries = match fs::read_dir(outbox) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("worker alert outbox unavailable".into()),
    };
    let mut paths: Vec<_> = entries
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| "could not read worker alert outbox")?
        .into_iter()
        .filter(|entry| {
            entry
                .file_name()
                .to_str()
                .is_some_and(host_link::worker_alert_filename)
        })
        .map(|entry| entry.path())
        .collect();
    paths.sort();
    for path in paths {
        host_command::output(
            Command::new("plangraph")
                .arg("host-link-deliver")
                .arg(&path),
            Duration::from_secs(12),
        )
        .map_err(|_| "Keeper did not acknowledge worker alert")?;
        match fs::remove_file(path) {
            Ok(()) => (),
            Err(error) if error.kind() == io::ErrorKind::NotFound => (),
            Err(_) => return Err("could not acknowledge delivered worker alert locally".into()),
        }
    }
    Ok(())
}

pub(crate) fn execute(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let mut flags = BTreeSet::new();
    if args.iter().any(|flag| {
        !matches!(flag.as_str(), "--deliver" | "--dispatch-rejoin") || !flags.insert(flag.as_str())
    }) {
        return Err("usage: pg-spawnd worker-liveness [--deliver] [--dispatch-rejoin]".into());
    }
    let registry = registry()?;
    let state = state()?;
    let mut failed = false;
    std::thread::scope(|scope| -> Result<(), String> {
        let tasks = registry_workers(&registry)
            .map(|host| {
                let registry = &registry;
                let state = &state;
                (host, scope.spawn(move || check(host, registry, state)))
            })
            .collect::<Vec<_>>();
        for (host, task) in tasks {
            let row = match task.join().expect("worker probe thread") {
                Ok((row, rejoin)) => {
                    if flags.contains("--dispatch-rejoin") && rejoin && !host.inventory_only() {
                        if let Err(error) = dispatch(&host.id) {
                            failed = true;
                            writeln!(out, "{}", json!({"host":host.id,"error":error}))
                                .map_err(|e| e.to_string())?;
                        }
                    }
                    row
                }
                Err(error) => {
                    failed = true;
                    json!({"host":host.id,"error":error})
                }
            };
            writeln!(out, "{row}").map_err(|error| error.to_string())?;
        }
        Ok(())
    })?;
    if flags.contains("--deliver") {
        deliver(&state)?;
    }
    if failed {
        Err("one or more worker liveness checks failed".into())
    } else {
        Ok(())
    }
}
