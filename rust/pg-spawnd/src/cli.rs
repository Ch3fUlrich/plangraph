//! Argument parsing and dispatch for the `pg-spawnd` CLI.

use pg_worker_core::host_probe::{HostProbe, HostProbeError};
use std::ffi::OsString;
use std::io::Write;

pub(crate) const USAGE: &str = "Usage: pg-spawnd <probe|serve|shim|health|agents-view> [args]";

/// `--help`/`-h` print usage and exit 0 only as the CLI's first argument or a
/// verb's first argument; anywhere else they are unknown flags and fail. As a
/// flag value (e.g. `--host-id -h`) they are that value.
fn is_help(arg: &str) -> bool {
    arg == "--help" || arg == "-h"
}

fn parse_probe(
    args: std::vec::IntoIter<String>,
    out: &mut dyn Write,
    reader: impl FnOnce(&str) -> Result<HostProbe, HostProbeError>,
) -> Result<(), String> {
    let mut json = false;
    let mut host_id = None;
    let mut args = args;
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
    writeln!(
        out,
        "{}",
        serde_json::to_string(&result).map_err(|e| e.to_string())?
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn parse_serve(args: std::vec::IntoIter<String>) -> Result<(), String> {
    let mut socket: Option<String> = None;
    let mut state: Option<String> = None;
    let mut options = crate::serve_options::Options::default();
    let mut seen = std::collections::HashSet::new();
    let mut args = args;
    while let Some(arg) = args.next() {
        if !seen.insert(arg.clone()) {
            return Err("unknown or duplicate flag".into());
        }
        match arg.as_str() {
            "--host-id" | "--max-runs" | "--max-queued" | "--placement" => {
                options.set(&arg, &args.next().ok_or("missing flag value")?)?;
            }
            "--no-scope" => options.no_scope = true,
            "--state" if state.is_none() => {
                state = Some(args.next().ok_or("missing or invalid --state")?);
            }
            "--socket" if socket.is_none() => {
                socket = Some(args.next().ok_or("missing or invalid --socket")?);
            }
            _ => return Err("unknown or duplicate flag".into()),
        }
    }
    let state = crate::state::resolve_state(state.as_deref(), |key| std::env::var_os(key))?;
    crate::serve::run(socket, state, options.resolve()?)
}

fn parse_shim(args: std::vec::IntoIter<String>) -> Result<(), String> {
    let mut socket: Option<String> = None;
    let mut args = args;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket" if socket.is_none() => {
                socket = Some(args.next().ok_or("missing or invalid --socket")?);
            }
            _ => return Err("unknown or duplicate flag".into()),
        }
    }
    crate::shim::run(socket)
}

pub(crate) fn execute(
    args: Vec<OsString>,
    out: &mut dyn Write,
    reader: impl FnOnce(&str) -> Result<HostProbe, HostProbeError>,
) -> Result<(), String> {
    let mut args = args
        .into_iter()
        .map(|arg| arg.into_string().map_err(|_| "non-UTF-8 argument"))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter();
    let first = args.next();
    if first.as_deref().is_some_and(is_help) {
        writeln!(out, "{USAGE}").map_err(|e| e.to_string())?;
        return Ok(());
    }
    let verb = first;
    let rest: Vec<String> = args.collect();
    if verb.as_deref() == Some("agents-view") {
        return crate::agent_view::execute(rest, out);
    }
    if verb.as_deref() == Some("health") {
        return crate::health_cli::execute(rest, out);
    }
    if matches!(verb.as_deref(), Some("probe" | "serve" | "shim"))
        && rest.first().is_some_and(|arg| is_help(arg))
    {
        writeln!(out, "{USAGE}").map_err(|e| e.to_string())?;
        return Ok(());
    }
    let rest = rest.into_iter();
    match verb.as_deref() {
        Some("probe") => parse_probe(rest, out, reader),
        Some("serve") => parse_serve(rest),
        Some("shim") => parse_shim(rest),
        _ => Err(USAGE.into()),
    }
}

pub(crate) fn run(
    args: impl IntoIterator<Item = OsString>,
    reader: impl FnOnce(&str) -> Result<HostProbe, HostProbeError>,
) -> i32 {
    let mut out = std::io::stdout();
    if let Err(error) = execute(args.into_iter().collect(), &mut out, reader) {
        eprintln!("{error}");
        return 2;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_probe(id: &str) -> Result<HostProbe, HostProbeError> {
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

    fn call(args: &[&str]) -> (Result<(), String>, String) {
        let mut out = Vec::new();
        let result = execute(
            args.iter().map(OsString::from).collect(),
            &mut out,
            fixture_probe,
        );
        (result, String::from_utf8(out).expect("utf-8"))
    }

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
    fn fixture_probe_succeeds_with_one_json_object() {
        let (result, out) = call(&["probe", "--json", "--host-id", "code"]);
        assert_eq!(result, Ok(()));
        assert!(out.ends_with('\n'), "one trailing newline");
        let body = out.trim_end_matches('\n');
        assert!(!body.contains('\n'), "exactly one line");
        let value: serde_json::Value = serde_json::from_str(body).expect("exactly one JSON object");
        assert!(value.is_object());
        assert_eq!(value["host_id"], "code");
        assert_eq!(value["os"], "fixture");
    }

    #[test]
    fn help_as_first_arg_prints_usage_and_exits_zero() {
        let (result, out) = call(&["--help"]);
        assert_eq!(result, Ok(()));
        assert!(out.contains(USAGE));
        let (result, out) = call(&["-h"]);
        assert_eq!(result, Ok(()));
        assert!(out.contains(USAGE));
    }

    #[test]
    fn help_as_verb_first_arg_prints_usage_and_exits_zero() {
        for verb in ["probe", "serve", "shim"] {
            for flag in ["--help", "-h"] {
                let (result, out) = call(&[verb, flag]);
                assert_eq!(result, Ok(()), "{verb} {flag}");
                assert!(out.contains(USAGE));
            }
        }
    }

    #[test]
    fn help_elsewhere_is_unknown_flag_exits_two() {
        for args in [
            &["probe", "--json", "--help"][..],
            &["probe", "--host-id", "code", "--help"][..],
            &["probe", "--json", "--host-id", "code", "-h"][..],
        ] {
            let (result, _) = call(args);
            assert_eq!(result, Err("unknown or duplicate flag".into()), "{args:?}");
            assert_eq!(run(args.iter().map(OsString::from), fixture_probe), 2);
        }
    }

    #[test]
    fn help_as_host_id_value_is_that_value() {
        for flag in ["-h", "--help"] {
            let args = ["probe", "--json", "--host-id", flag];
            let (result, out) = call(&args);
            assert_eq!(result, Err("missing or invalid --host-id".into()));
            assert!(out.is_empty(), "no usage for {flag} as value");
        }
    }

    #[test]
    fn unknown_verb_exits_two_with_usage() {
        let (result, _) = call(&["frobnicate"]);
        assert_eq!(result, Err(USAGE.into()));
        let (result, _) = call(&[]);
        assert_eq!(result, Err(USAGE.into()));
    }
}
