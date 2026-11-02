use pg_worker_core::host_probe::{probe, HostProbe, HostProbeError};
use std::ffi::OsString;

const USAGE: &str = "Usage: pg-spawnd probe --json --host-id <id>";

fn execute(
    args: Vec<OsString>,
    reader: impl FnOnce(&str) -> Result<HostProbe, HostProbeError>,
) -> Result<(), String> {
    if args.iter().any(|arg| arg == "--help" || arg == "-h") {
        println!("{USAGE}");
        return Ok(());
    }
    let mut args = args
        .into_iter()
        .map(|arg| arg.into_string().map_err(|_| "non-UTF-8 argument"))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter();
    let verb = args.next();
    if verb.as_deref() != Some("probe") {
        return Err(USAGE.into());
    }
    let mut json = false;
    let mut host_id = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--json" if !json => json = true,
            "--host-id" if host_id.is_none() => {
                let value = args.next().ok_or("missing or invalid --host-id")?;
                if value.starts_with('-') {
                    return Err("missing or invalid --host-id".into());
                }
                host_id = Some(value);
            }
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
    let result = reader(&id).map_err(|e| e.to_string())?;
    println!(
        "{}",
        serde_json::to_string(&result).map_err(|e| e.to_string())?
    );
    Ok(())
}

fn run(
    args: impl IntoIterator<Item = OsString>,
    reader: impl FnOnce(&str) -> Result<HostProbe, HostProbeError>,
) -> i32 {
    if let Err(error) = execute(args.into_iter().collect(), reader) {
        eprintln!("{error}");
        return 2;
    }
    0
}

fn main() {
    std::process::exit(run(std::env::args_os().skip(1), probe));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_error_exits_two() {
        assert_eq!(
            run(
                ["probe", "--json", "--host-id", "code"].map(OsString::from),
                |_| { Err(HostProbeError::Invalid("MemTotal:")) }
            ),
            2
        );
    }

    #[test]
    fn fixture_probe_succeeds() {
        assert_eq!(
            run(
                ["probe", "--json", "--host-id", "code"].map(OsString::from),
                |id| {
                    Ok(HostProbe {
                        host_id: id.into(),
                        os: "fixture".into(),
                        mem_total_mb: 2,
                        mem_available_mb: 1,
                        swap_total_mb: 0,
                        swap_free_mb: 0,
                        cpus: 1,
                        load1: None,
                        load5: None,
                        load15: None,
                        ts_ms: 0,
                    })
                }
            ),
            0
        );
    }
}
