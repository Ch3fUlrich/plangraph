use pg_worker_core::{
    fleet_collect,
    fleet_series::{self, Sample},
};
use std::{
    env, fs, io,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

fn run() -> io::Result<()> {
    let mut args = env::args().skip(1);
    let verb = args.next().unwrap_or_default();
    let mut host = env::var("PLANGRAPH_HOST_NAME").unwrap_or_default();
    let mut state = env::var_os("HOME")
        .map(PathBuf::from)
        .map(|p| p.join(".local/state/plangraph"));
    let mut ssh = None;
    let mut hours = 24u64;
    while let Some(flag) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        match flag.as_str() {
            "--host" => host = value,
            "--state" => state = Some(value.into()),
            "--ssh" => ssh = Some(value),
            "--hours" => hours = value.parse().map_err(io::Error::other)?,
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    let state = state.ok_or_else(|| io::Error::other("HOME or --state required"))?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs();
    match verb.as_str() {
        "sample" | "probe" => {
            if !fleet_series::valid_host(&host) { return Err(io::Error::other("--host required")); }
            let result = match ssh { Some(dest) => fleet_collect::remote(&host, &dest), None => fleet_collect::collect(&host, &state) };
            let mut row = match result {
                Ok(row) => row,
                Err(e) if verb == "sample" => {
                    eprintln!("host probe unavailable: {e}");
                    Sample { host, ts: now, ..Sample::default() }
                },
                Err(e) => return Err(e),
            };
            if verb == "sample" { fleet_series::append(&state.join("fleet"), &mut row)?; }
            println!("{}", serde_json::to_string(&row)?);
        }
        "summary" => {
            let mut rows = Vec::new();
            for entry in fs::read_dir(state.join("fleet"))? {
                let path = entry?.path();
                if path.extension().is_some_and(|e| e == "ndjson") { rows.extend(fleet_series::read(&path)?); }
            }
            println!("{}", serde_json::to_string_pretty(&fleet_series::summarize(&rows, now.saturating_sub(hours.saturating_mul(3600)), now))?);
        }
        _ => return Err(io::Error::other("usage: pg-fleet-series sample|probe|summary [--host ID] [--state DIR] [--ssh DEST] [--hours N]")),
    }
    Ok(())
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            std::process::ExitCode::FAILURE
        }
    }
}
