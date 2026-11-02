use pg_worker_core::host_probe::probe;

const USAGE: &str = "Usage: pg-spawnd probe --json --host-id <id>";

fn run() -> Result<(), String> {
    let mut args = std::env::args().skip(1);
    let verb = args.next();
    if verb.as_deref() == Some("--help") && args.len() == 0 {
        println!("{USAGE}");
        return Ok(());
    }
    if verb.as_deref() != Some("probe") {
        return Err(USAGE.into());
    }
    let mut json = false;
    let mut host_id = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--json" if !json => json = true,
            "--host-id" if host_id.is_none() => host_id = args.next(),
            _ => return Err("unknown or duplicate flag".into()),
        }
    }
    let id = host_id
        .filter(|s| {
            (1..=32).contains(&s.len())
                && s.as_bytes()[0].is_ascii_lowercase()
                && s.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
        .ok_or("missing or invalid --host-id")?;
    if !json {
        return Err("missing --json".into());
    }
    let result = probe(&id).map_err(|e| e.to_string())?;
    println!(
        "{}",
        serde_json::to_string(&result).map_err(|e| e.to_string())?
    );
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
