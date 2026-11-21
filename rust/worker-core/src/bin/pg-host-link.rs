//! One foreground host-scheduler tick; delivery uses the existing watchdog bridge.
use pg_worker_core::{
    host_command::{output, ssh},
    host_link::{observe, observe_ssh, ssh_down_since},
};
use serde_json::{json, Value};
use std::{
    io,
    path::Path,
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

fn probe(peer: &str) -> io::Result<bool> {
    match output(
        Command::new("tailscale").args([
            "ping",
            "--c=1",
            "--timeout=5s",
            "--until-direct=false",
            peer,
        ]),
        Duration::from_secs(10),
    ) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == io::ErrorKind::Other => Ok(false),
        Err(e) => Err(e),
    }
}

fn peer_state(peer: &str) -> Option<Value> {
    let bytes = output(
        Command::new("tailscale").args(["status", "--json"]),
        Duration::from_secs(3),
    )
    .ok()?;
    let status: Value = serde_json::from_slice(&bytes).ok()?;
    status["Peer"]
        .as_object()?
        .values()
        .find(|node| {
            node["HostName"] == peer
                || node["DNSName"]
                    .as_str()
                    .is_some_and(|name| name.trim_end_matches('.') == peer.trim_end_matches('.'))
                || node["TailscaleIPs"]
                    .as_array()
                    .is_some_and(|ips| ips.iter().any(|ip| ip == peer))
        })
        .map(
            |node| json!({"Online":node["Online"], "Active":node["Active"], "Relay":node["Relay"]}),
        )
}

fn last_ip_change() -> Option<String> {
    let bytes = output(
        Command::new("journalctl").args([
            "--unit=tailscaled.service",
            "--grep=gateway and self IP changed",
            "--lines=1",
            "--no-pager",
            "--output=short-iso",
        ]),
        Duration::from_secs(3),
    )
    .ok()?;
    String::from_utf8_lossy(&bytes)
        .lines()
        .find(|line| line.contains("gateway and self IP changed"))
        .map(|line| line.chars().take(512).collect())
}

fn ssh_tick(
    root: &Path,
    observer: &str,
    host: &str,
    destination: &str,
    peer: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let reachable = match output(ssh(destination)?.arg("true"), Duration::from_secs(7)) {
        Ok(_) => true,
        Err(err) if err.kind() == io::ErrorKind::Other => false,
        Err(err) => return Err(err.into()),
    };
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    let mut evidence = Value::Null;
    if let Some(since) = observe_ssh(root, destination, now, reachable, || {
        evidence = json!({
            "host": host,
            "observer": observer,
            "peer": peer,
            "peer_state": if reachable { None } else { peer_state(peer) },
            "last_ip_change": if reachable { None } else { last_ip_change() },
        });
        evidence.clone()
    })? {
        if reachable {
            println!("{host} recovered (unreachable since {since})");
        } else {
            println!("{host} unreachable since {since} {evidence}");
        }
    }
    Ok(())
}

fn execute() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if let [verb, root, observer, host, destination, peer] = args.as_slice() {
        if verb == "ssh-tick" {
            return ssh_tick(Path::new(root), observer, host, destination, peer);
        }
    }
    if let [verb, root, destination] = args.as_slice() {
        if verb == "ssh-status" {
            if let Some(since) = ssh_down_since(Path::new(root), destination)? {
                return Err(format!("{destination} unreachable since {since}").into());
            }
            return Ok(());
        }
    }
    if args.len() != 3
        || args[1].is_empty()
        || args[2].is_empty()
        || args[2].starts_with('-')
        || !args[2]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".:-".contains(&b))
    {
        return Err("usage: pg-host-link STATE_ROOT HOST_ID TAILSCALE_PEER".into());
    }
    let reachable = probe(&args[2])?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    let queued = observe(Path::new(&args[0]), &args[1], &args[2], now, reachable)?;
    println!("host-link reachable={reachable} alert_queued={queued}");
    Ok(())
}

fn main() {
    if let Err(e) = execute() {
        eprintln!("host-link: {e}");
        std::process::exit(1);
    }
}
