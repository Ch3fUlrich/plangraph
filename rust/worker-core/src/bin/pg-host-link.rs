//! One foreground host-scheduler tick; delivery uses the existing watchdog bridge.
use pg_worker_core::{host_command::output, host_link::observe};
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

fn execute() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
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
