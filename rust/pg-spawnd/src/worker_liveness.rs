//! Fleet registry polling and delivery through the coordinator's existing watchdog bridge.

use pg_worker_core::{
    host_command, host_link,
    hosts::{HostClass, HostEntry, HostRegistry, Transport},
    run::{record::valid_run_id, record_io::ensure_private_dir},
    worker_host::{Observation, Store},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    env, fs, io,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

fn now() -> Result<u64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .map_err(|_| "system clock predates epoch".into())
}

pub(crate) fn registry() -> Result<HostRegistry, String> {
    crate::state::host_registry()
}

pub(crate) fn remote(host: &HostEntry) -> Result<Command, String> {
    let route = host.ssh_route().ok_or("worker has no SSH route")?;
    let mut command = host_command::ssh(&route).map_err(|_| "worker SSH route is invalid")?;
    if let Some(distro) = host.wsl_distro.as_deref() {
        if !host_command::valid_wsl_distro(distro) {
            return Err("invalid worker WSL distribution".into());
        }
        command.args(["wsl.exe", "--distribution", distro, "--exec"]);
    }
    Ok(command)
}

fn active_run_ids(state: &Path, registry: &HostRegistry, host: &str) -> BTreeSet<String> {
    crate::records::list_runs(state)
        .into_iter()
        .filter(|row| {
            matches!(
                row["status"].as_str(),
                Some("running" | "queued" | "unknown")
            )
        })
        .filter(|row| {
            row["host"]
                .as_str()
                .and_then(|name| registry.resolve(name))
                .is_some_and(|entry| entry.id == host)
        })
        .filter_map(|row| {
            row["run_id"]
                .as_str()
                .filter(|id| valid_run_id(id))
                .map(str::to_owned)
        })
        .collect()
}

pub(crate) fn state() -> Result<PathBuf, String> {
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
    let belongs = |row: &Value| {
        row["host"]
            .as_str()
            .and_then(|host| registry.resolve(host))
            .is_some_and(|entry| entry.id == *name)
    };
    let found = crate::worker_inventory::scan_host(&state, belongs);
    // `runs` stays every active record; `live` and `dead` say which have a session.
    let runs = active_run_ids(&state, &registry, name);
    writeln!(
        out,
        "{}",
        json!({"host":name,"runs":runs,"live":found.live,"dead":found.dead,"terminal":found.terminal})
    )
    .map_err(|error| error.to_string())
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

fn check(host: &HostEntry, registry: &HostRegistry, state: &Path) -> Result<Value, String> {
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
    let phase = Store::new(state, &host.id)
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
    Ok(json!({"host":host.id,"ssh":reachable,"tailscale":tailscale,
        "inventory_available":inventory_available,"phase":phase}))
}

fn deliver(state: &Path, out: &mut dyn Write) -> Result<(), String> {
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
    let rejected = state.join("watchdog/rejected");
    let mut failed = false;
    for path in paths {
        let name = path.file_name().expect("outbox entry has a name");
        if !path.exists() {
            continue;
        }
        let result = host_command::capture(
            Command::new("plangraph")
                .arg("host-link-deliver")
                .arg(&path),
            Duration::from_secs(12),
        );
        let (reason, permanent, action) = match result.ok().and_then(|reply| reply.status.code()) {
            Some(0) => (
                "could not acknowledge delivered worker alert locally",
                false,
                fs::remove_file(&path),
            ),
            Some(host_link::WORKER_ALERT_REFUSED_EXIT) => (
                "Keeper refused worker alert",
                true,
                ensure_private_dir(&rejected).and_then(|()| fs::rename(&path, rejected.join(name))),
            ),
            _ => (
                "Keeper did not acknowledge worker alert",
                false,
                Err(io::Error::other("delivery failed")),
            ),
        };
        match action {
            Err(error) if error.kind() == io::ErrorKind::NotFound && !path.exists() => continue,
            Ok(()) if !permanent => continue,
            action => {
                failed = true;
                writeln!(
                    out,
                    "{}",
                    json!({"outbox_file":name.to_string_lossy(), "error":reason,
                    "quarantined":permanent && action.is_ok()})
                )
                .map_err(|error| error.to_string())?;
            }
        }
    }
    if failed {
        Err("one or more worker alerts failed delivery".into())
    } else {
        Ok(())
    }
}

pub(crate) fn execute(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let deliver_alerts = match args {
        [] => false,
        [flag] if flag == "--deliver" => true,
        _ => return Err("usage: pg-spawnd worker-liveness [--deliver]".into()),
    };
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
                Ok(row) => row,
                Err(error) => {
                    failed = true;
                    json!({"host":host.id,"error":error})
                }
            };
            writeln!(out, "{row}").map_err(|error| error.to_string())?;
        }
        Ok(())
    })?;
    if deliver_alerts {
        deliver(&state, out)?;
    }
    if failed {
        Err("one or more worker liveness checks failed".into())
    } else {
        Ok(())
    }
}
