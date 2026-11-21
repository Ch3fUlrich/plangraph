//! One foreground host-scheduler tick; delivery uses the existing watchdog bridge.
use pg_worker_core::{
    host_command::{capture, output, ssh},
    host_link::{observe, observe_ssh, ssh_down_since},
};
use serde_json::{json, Value};
use std::{
    io::{self, Write},
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
    let reachable = match capture(ssh(destination)?.arg("exit 0"), Duration::from_secs(7)) {
        Ok(result) => match result.status.code() {
            Some(code) => code != 255,
            None => return Err("SSH probe was interrupted".into()),
        },
        Err(err) if err.kind() == io::ErrorKind::TimedOut => false,
        Err(err) => return Err(err.into()),
    };
    let now = unix_seconds()?;
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

fn unix_seconds() -> Result<u64, std::time::SystemTimeError> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}

fn ssh_wait(
    root: &Path,
    destination: &str,
    argv: &[String],
) -> Result<i32, Box<dyn std::error::Error>> {
    const ATTEMPTS: usize = 3;
    let mut cmd = ssh(destination)?;
    cmd.args(argv);
    let mut first_failure = unix_seconds()?;
    for attempt in 0..ATTEMPTS {
        if let Some(since) = ssh_down_since(root, destination)? {
            first_failure = first_failure.min(since);
        } else {
            match capture(&mut cmd, Duration::from_secs(7)) {
                Ok(result) if result.status.code() != Some(255) => {
                    io::stdout().write_all(&result.stdout)?;
                    io::stderr().write_all(&result.stderr)?;
                    return Ok(result.status.code().unwrap_or(1));
                }
                Ok(_) => (),
                Err(error) if error.kind() == io::ErrorKind::TimedOut => (),
                Err(error) => return Err(error.into()),
            }
        }
        if attempt + 1 < ATTEMPTS {
            std::thread::sleep(Duration::from_secs(2));
        }
    }
    eprintln!("{destination} unreachable since {first_failure} ({ATTEMPTS} checks)");
    Ok(255)
}

fn execute() -> Result<i32, Box<dyn std::error::Error>> {
    const USAGE: &str = "usage: pg-host-link STATE_ROOT HOST_ID TAILSCALE_PEER | ssh-tick STATE_ROOT OBSERVER HOST_ID DESTINATION PEER | ssh-status STATE_ROOT DESTINATION | ssh-wait STATE_ROOT DESTINATION -- COMMAND [ARG...]";
    let args: Vec<_> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [verb, root, observer, host, destination, peer]
            if verb == "ssh-tick"
                && [root, observer, host, peer]
                    .iter()
                    .all(|arg| !arg.is_empty()) =>
        {
            ssh_tick(Path::new(root), observer, host, destination, peer).map(|()| 0)
        }
        [verb, root, destination, separator, argv @ ..]
            if verb == "ssh-wait" && !root.is_empty() && separator == "--" && !argv.is_empty() =>
        {
            ssh_wait(Path::new(root), destination, argv)
        }
        [verb, root, destination] if verb == "ssh-status" && !root.is_empty() => {
            if let Some(since) = ssh_down_since(Path::new(root), destination)? {
                return Err(format!("{destination} unreachable since {since}").into());
            }
            Ok(0)
        }
        [root, host, peer]
            if !root.is_empty()
                && !matches!(root.as_str(), "ssh-tick" | "ssh-status" | "ssh-wait")
                && !host.is_empty()
                && !peer.is_empty()
                && !peer.starts_with('-')
                && peer
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b".:-".contains(&b)) =>
        {
            let reachable = probe(peer)?;
            let queued = observe(Path::new(root), host, peer, unix_seconds()?, reachable)?;
            println!("host-link reachable={reachable} alert_queued={queued}");
            Ok(0)
        }
        _ => Err(USAGE.into()),
    }
}

fn main() {
    let code = execute().unwrap_or_else(|e| {
        eprintln!("host-link: {e}");
        1
    });
    std::process::exit(code);
}
